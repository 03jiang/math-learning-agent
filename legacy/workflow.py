"""数学协调层：独立任务、六项设置、模拟工具请求、每轮去重与本地日志。"""
from dataclasses import asdict, dataclass, field, replace
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from legacy.core import (LearningAssistant, LearningSettings, Metadata, PendingUpdate, SETTING_OPTIONS,
                  STATE_FIELDS, SimulatedReply, Snapshot, TaskState, ValidationError, validate_patch)
from legacy.retrieval import NOTES_DIR, ToolBudget, check_deadline

from legacy.curriculum import TASKS, MockTutor, assess_submission, initial_snapshot
from legacy.model_boundary import reading_context


@dataclass
class RoundResult:
    request_id: str
    task_id: str
    effective_settings: dict
    reply: SimulatedReply | None
    proposal: PendingUpdate | None
    sources: list
    tool_records: list
    error: str | None = None
    log_warning: str | None = None
    local_checks: dict = field(default_factory=dict)
    model_calls: list = field(default_factory=list)

    @property
    def real_api_calls(self):
        """请求尝试计数；超时不代表服务端未处理，也不等同于计费次数。"""
        return sum(row['attempted_requests'] for row in self.model_calls if row['kind'] == 'real_api')


class Workspace:
    def __init__(self, data_dir, *, notes_dir=NOTES_DIR, tutor=None):
        self.data_dir = Path(data_dir)
        self.notes_dir = notes_dir
        self.tutor = tutor or MockTutor()
        self.assistants = {task_id: LearningAssistant(self.data_dir / f'{task_id}.json', initial_snapshot(task_id))
                           for task_id in TASKS}
        self.rounds = {}
        self.fingerprints = {}
        self.generation_count = 0

    def assistant(self, task_id):
        if task_id not in TASKS:
            raise ValidationError('未知任务')
        return self.assistants[task_id]

    def log(self, record):
        """每次运行一行；失败明确提示，不自动重跑生成。日志不是已确认状态。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        row = {'timestamp': datetime.now(timezone.utc).isoformat(), 'code_version': 'primary-math-api-v2', **record}
        with (self.data_dir / 'runs.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    def run(self, request_id, task_id, message, overrides=None, *, answer_submission=None, solution_steps='',
            reading_selections=None, help_action=None, search_query=None):
        if type(message) is not str or len(message) > 2000:
            raise ValidationError('学习困难须为不超过 2000 字符的文本')
        if answer_submission is not None and (type(answer_submission) is not str or len(answer_submission) > 80):
            raise ValidationError('答案须为不超过 80 字符的文本')
        if type(solution_steps) is not str or len(solution_steps) > 2000:
            raise ValidationError('步骤须为不超过 2000 字符的文本')
        if help_action not in (None, 'hint', 'direct', 'next'):
            raise ValidationError('未知求助操作')
        if search_query is not None and (type(search_query) is not str or not search_query.strip() or len(search_query) > 200):
            raise ValidationError('笔记关键词须为 1 至 200 字符')
        reading = reading_context(task_id, reading_selections)  # 重算选择结果，不信任外部自填的判断
        if not message.strip() and not (answer_submission or '').strip() and not solution_steps.strip() and not (help_action or search_query or reading):
            raise ValidationError('请填写困难、答案或解题步骤')
        if type(request_id) is not str or not request_id:
            raise ValidationError('缺少本轮 ID')
        overrides = {} if overrides is None else overrides
        if overrides:
            validate_patch(overrides, SETTING_OPTIONS.keys(), True)
        elif type(overrides) is not dict:
            raise ValidationError('本轮设置必须是字典')
        fingerprint = json.dumps([task_id, message, overrides, answer_submission, solution_steps, reading_selections,
                                  help_action, search_query], sort_keys=True, ensure_ascii=False)
        if request_id in self.rounds:
            if fingerprint != self.fingerprints[request_id]:
                raise ValidationError('同一轮 ID 不能用于其他输入或任务')
            return self.rounds[request_id]
        assistant = self.assistant(task_id)
        snapshot = assistant.snapshot()
        values = {**asdict(snapshot.settings), **overrides}
        # 仅对明确短语作规则解析，均属于本轮设置；日志记录真正使用的值。
        if '直接' in message:
            values['explanation_mode'] = 'direct'
        elif '小提示' in message:
            values['explanation_mode'] = 'hint'
        if '详细' in message:
            values['presentation_density'] = 'detailed'
        if help_action in ('hint', 'direct', 'next'):
            values['explanation_mode'] = 'direct' if help_action == 'direct' else 'hint'
        settings = LearningSettings(**values)
        budget = ToolBudget(self.notes_dir)
        start = time.monotonic()
        result = RoundResult(request_id, task_id, asdict(settings), None, None, [], budget.records)
        # 在副作用开始前登记本轮，失败也缓存，重绘不会再次执行。
        self.rounds[request_id] = result
        self.fingerprints[request_id] = fingerprint
        self.generation_count += 1
        answer, steps = assess_submission(task_id, message, answer_submission, solution_steps)
        result.local_checks = {'answer': asdict(answer) if answer else None,
                               'steps': steps.to_dict() if steps else None, 'reading': reading}
        call_start = len(getattr(self.tutor, 'call_records', []))
        try:
            calls = [('search_notes', {'query': search_query})] if search_query is not None else self.tutor.plan(message)
            for name, arguments in calls:
                result.sources.extend(budget.call(name, arguments))
            result.sources = list({item['source_id']: item for item in result.sources}.values())
            check_deadline(budget.deadline)  # 检索时限在调用模型前结算
            if answer_submission is None and not solution_steps and reading_selections is None and help_action is None:
                result.reply = self.tutor.answer(message, snapshot, settings, result.sources, bool(calls))
            else:
                result.reply = self.tutor.answer(message, snapshot, settings, result.sources, bool(calls),
                                                answer_submission=answer_submission, solution_steps=solution_steps,
                                                **({'reading_selections': reading_selections} if reading_selections is not None else {}),
                                                **({'help_action': help_action} if help_action is not None else {}))
            # 回复来源不能自称“检查通过”；本地结果始终为准，失败时也单独保留。
            result.reply = replace(result.reply, answer_check=result.local_checks['answer'],
                                   step_check=result.local_checks['steps'])
            if not getattr(self.tutor, 'uses_http', False):
                check_deadline(budget.deadline)
            for proposed, allowed, is_setting in (
                (result.reply.proposed_state_update, STATE_FIELDS, False),
                (result.reply.proposed_configuration_update, SETTING_OPTIONS.keys(), True),
            ):
                if proposed is not None:
                    validate_patch(proposed, allowed, is_setting)
            if assistant.snapshot().metadata.version != snapshot.metadata.version:
                raise ValidationError('生成期间任务已更新，请重新提交')
            if result.reply.proposed_state_update is not None or result.reply.proposed_configuration_update is not None:
                result.proposal = assistant.propose(result.reply.proposed_state_update, result.reply.proposed_configuration_update)
        except (OSError, ValueError, TimeoutError) as exc:
            result.error = str(exc)
            result.reply = None
            result.proposal = None
        finally:
            result.model_calls = deepcopy(getattr(self.tutor, 'call_records', [])[call_start:])
        try:
            self.log({'event': 'round', 'model': self.tutor.name, 'request_id': request_id,
                      'task_id': task_id, 'input': message, 'input_state': asdict(snapshot),
                      'answer_submission': answer_submission, 'solution_steps': solution_steps,
                      'reading_selections': deepcopy(reading_selections), 'help_action': help_action, 'search_query': search_query,
                      'effective_settings': result.effective_settings, 'tools': result.tool_records,
                      'local_checks': result.local_checks,
                      'model_calls': result.model_calls,
                      'reply': asdict(result.reply) if result.reply else None,
                      'proposal': asdict(result.proposal) if result.proposal else None,
                      'error': result.error, 'elapsed_ms': round((time.monotonic() - start) * 1000, 2),
                      'real_api_calls': result.real_api_calls})
        except OSError as exc:
            result.log_warning = f'运行日志未保存：{exc}'
        return result

    def decide(self, task_id, proposal, action, *, edited_state=None, edited_configuration=None):
        assistant = self.assistant(task_id)
        if proposal.task_id != task_id:
            raise ValidationError('不能处理其他任务的建议')
        if action == 'reject':
            assistant.reject(proposal)
            status = 'rejected'
        elif action in {'accept', 'edit'}:
            status = assistant.confirm(proposal, edited_state=edited_state, edited_configuration=edited_configuration)
        else:
            raise ValidationError('未知确认操作')
        warning = None
        if status != 'already_applied':
            try:
                self.log({'event': 'decision', 'task_id': task_id, 'proposal_id': proposal.proposal_id,
                          'action': action, 'status': status, 'state': asdict(assistant.snapshot())})
            except OSError as exc:
                warning = f'状态处理成功，但确认日志未保存：{exc}'
        return status, warning
