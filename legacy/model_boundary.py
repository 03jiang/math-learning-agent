"""供应商无关的请求/回复边界与离线回放。没有 HTTP 客户端，不读取密钥。"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from legacy.core import SETTING_OPTIONS, SimulatedReply, ValidationError, validate_patch
from legacy.curriculum import ANSWERS, QUESTIONS, STEPS, MockTutor, assess_submission
from legacy.reading_check import check_reading
from legacy.teaching_guard import validate_teaching_contract

CONTRACT_VERSION = 1
PROMPT_VERSION = 'primary-math-contract-v5'
MAX_RESPONSE_BYTES = 16384
RESPONSE_FIELDS = {'schema_version', 'explanation', 'next_action', 'optional_hint',
                   'proposed_state_update', 'proposed_configuration_update', 'cited_source_ids'}
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

def prompt_text(variant):
    if variant not in ("baseline", "protocol"):
        raise ValidationError("未知提示词版本")
    return (PROMPTS_DIR / f"{variant}.txt").read_text(encoding="utf-8") + "\n" + (PROMPTS_DIR / "output_contract.txt").read_text(encoding="utf-8")

INSTRUCTIONS = prompt_text("protocol")


def response_example(context):
    """只用本地允许步骤生成格式示例；不补造实际模型回复或待确认建议。"""
    action = None
    current = context['task']['current_step']
    allowed = context['allowed_next_actions']
    if context.get('requested_help') == 'next' and allowed:
        action = current if current in allowed else allowed[0]
    explanation = (f'建议先做：{action}。这只是待确认建议，确认前不会保存进度。' if action
                   else '请先说明你对题目的理解，或补充可以核对的步骤。')
    return {'schema_version': CONTRACT_VERSION, 'explanation': explanation, 'next_action': action,
            'optional_hint': None,
            'proposed_state_update': {'current_step': action} if action and action != current else None,
            'proposed_configuration_update': None, 'cited_source_ids': []}


def progress_blocked(answer, steps):
    return bool((answer and answer.status in {'invalid', 'invalid_unit'})
                or (steps and steps.status == 'needs_review'))


def build_model_request(message, snapshot, settings, sources, *, answer_submission=None, solution_steps='',
                        reading_selections=None, prompt_variant='protocol', help_action=None):
    """仅组织当前任务上下文。不会扫描存档、日志、环境变量或其他任务。"""
    task_id = snapshot.metadata.task_id
    answer, steps = assess_submission(task_id, message, answer_submission, solution_steps)
    reading = reading_context(task_id, reading_selections)
    request = {
        'contract_version': 2,
        'prompt_version': PROMPT_VERSION if prompt_variant == 'protocol' else 'ordinary-math-baseline-v3',
        'instructions': prompt_text(prompt_variant),
        'response_fields': sorted(RESPONSE_FIELDS),
        'context': {
            'task_id': task_id,
            'task_version': snapshot.metadata.version,
            'question': QUESTIONS[task_id],
            'task': deepcopy(asdict(snapshot.task)),
            'effective_settings': asdict(settings),
            'setting_options': {name: list(values) for name, values in SETTING_OPTIONS.items()},
            'student_input': {'difficulty': message, 'answer': answer_submission, 'steps': solution_steps},
            'requested_help': help_action,
            'reading_check': reading,
            'local_checks': {'answer': asdict(answer) if answer else None,
                             'steps': steps.to_dict() if steps else None},
            'reference_answer': str(ANSWERS[task_id]),
            'allowed_next_actions': [] if progress_blocked(answer, steps) else list(STEPS[task_id]),
            'sources': [{key: item[key] for key in ('source_id', 'title', 'snippet')} for item in sources],
        },
    }
    request['instructions'] += (
        '\n本轮格式示例（步骤与解释仅为示范；按上下文选择合适步骤。'
        '提出 proposed_state_update 不等于保存，等用户确认不是省略建议的理由）：\n'
        + json.dumps(response_example(request['context']), ensure_ascii=False))
    return request


def reading_context(task_id, selections):
    if selections is None:
        return None
    report = asdict(check_reading(task_id, selections))
    report['incorrect_question_ids'] = [row['key'] for row in report['rows'] if row['status'] == 'incorrect']
    report['unanswered_question_ids'] = [row['key'] for row in report['rows'] if row['status'] == 'unanswered']
    return report


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError('模型回复包含重复 JSON 字段')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValidationError('模型回复包含非标准 JSON 数值')


def _text(value, maximum):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\x00' in value:
        return False
    try:
        value.encode('utf-8')
    except UnicodeError:
        return False
    return True


def parse_model_response(raw, request):
    """拒绝未知字段和越权建议。不修补坏 JSON，不信任回复自称的校验或来源。"""
    if type(raw) is not str:
        raise ValidationError('模型回复必须是 JSON 文本')
    try:
        if len(raw.encode('utf-8')) > MAX_RESPONSE_BYTES:
            raise ValidationError('模型回复超过 16 KiB 限制')
        data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ValidationError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError('模型回复不是合法 JSON 对象') from None
    if type(data) is not dict or set(data) != RESPONSE_FIELDS:
        raise ValidationError('模型回复字段缺失或含未允许字段')
    if type(data['schema_version']) is not int or data['schema_version'] != CONTRACT_VERSION:
        raise ValidationError('模型回复协议版本不受支持')
    if not _text(data['explanation'], 3000):
        raise ValidationError('模型解释必须是 1 至 3000 字符的非空文本')
    if data['optional_hint'] is not None and not _text(data['optional_hint'], 1000):
        raise ValidationError('模型提示必须为 null 或 1 至 1000 字符的非空文本')
    context = request['context']
    action = data['next_action']
    if action is not None and (type(action) is not str or action not in context['allowed_next_actions']):
        raise ValidationError('模型建议了当前不允许的下一步')
    state = data['proposed_state_update']
    if state is not None:
        if (type(state) is not dict or set(state) != {'current_step'}
                or action is None or state['current_step'] != action):
            raise ValidationError('模型进度建议只能包含与下一步一致的 current_step')
    config = data['proposed_configuration_update']
    if config is not None:
        validate_patch(config, SETTING_OPTIONS.keys(), settings=True)
    citations = data['cited_source_ids']
    available = {item['source_id'] for item in context['sources']}
    if (type(citations) is not list or len(citations) > len(available)
            or any(type(item) is not str or item not in available for item in citations)
            or len(citations) != len(set(citations))):
        raise ValidationError('模型引用了未提供的、重复的或无效的来源 ID')
    validate_teaching_contract(data, context)
    # 同一步无需再次写入；这不表示该步骤已完成。
    if state and state['current_step'] == context['task']['current_step']:
        data['proposed_state_update'] = None
    return data


class ReplayTutor:
    """将一份预设 JSON 当作外部回复，通过真实校验与确认链路；不是语言模型。"""
    name = 'offline-model-replay-v1'

    def __init__(self, response_json):
        self.response_json = response_json
        self.requests = []

    def plan(self, message):
        # 本轮仍沿用明确短语触发的只读检索；回放内容不能请求工具。
        return MockTutor().plan(message)

    def answer(self, message, snapshot, settings, sources, searched, *, answer_submission=None, solution_steps='',
               reading_selections=None, help_action=None):
        request = build_model_request(message, snapshot, settings, sources,
                                      answer_submission=answer_submission, solution_steps=solution_steps,
                                      reading_selections=reading_selections, help_action=help_action)
        self.requests.append(deepcopy(request))
        parsed = parse_model_response(self.response_json, request)
        checks = request['context']['local_checks']
        return SimulatedReply(parsed['explanation'], parsed['next_action'] or '先核对输入，或说明还需要哪种帮助。',
                              parsed['optional_hint'], parsed['proposed_state_update'],
                              parsed['proposed_configuration_update'],
                              '离线模型回复回放 · 预设 JSON，无真实 API',
                              answer_check=deepcopy(checks['answer']), step_check=deepcopy(checks['steps']),
                              cited_source_ids=parsed['cited_source_ids'], origin='offline_replay')
