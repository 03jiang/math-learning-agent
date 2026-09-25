"""Frozen A/B preflight and local HTTP rehearsal. This entry point cannot call a paid API."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time

from legacy.http_test_support import LocalModelServer
from study.agent import AgentStudyService, Limits
from study.agent_protocol import AgentError
from study.agent_tools import ToolScope
from study.context import COACH_PROMPT, remember, select_next
from study.evaluation_baseline import FixedWorkflowService, rule_queries
from study.evaluation_cases import ASSETS, ROOT, inventory, model_context, selected_cases, task_for_turn
from study.evidence import ANSWER_ONLY_FEEDBACK
from study.notebook import Notebook, make_entry
from study.preferences import LocalPreferences
from study.run_audit import AuditError, digest, read_json, stamp, write_json
from study.service import ANALYSIS_PROMPT, PhotoTransport
from study.smoke import default_config, source_snapshot

VERSION = 'agent-ab-offline-v1'
DIMENSIONS = ('math_steps', 'help_fit', 'tool_use', 'source_support', 'history_use')


def asset_hashes():
    paths = [*ASSETS.rglob('*'), *ROOT.glob('notes/*.json')]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file()}


def freeze(directory, split='development'):
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise AuditError('Output already exists; it will not be overwritten.')
    cases = selected_cases(split)
    rows = []
    for i, case in enumerate(cases):
        # Alternate order, preserve all turns in each conversation.
        for arm in ('A', 'B') if i % 2 == 0 else ('B', 'A'):
            for turn in range(len(case['turns'])):
                rows.append({'row_id': f"{case['case_id']}-{arm}-t{turn + 1:02d}",
                             'case_id': case['case_id'], 'arm': arm, 'turn_index': turn})
    manifest = {'schema_version': 1, 'suite': VERSION, 'created_at': stamp(), 'split': split,
                'cases': cases, 'rows': rows, 'inventory': inventory(cases),
                'code': source_snapshot(), 'assets': asset_hashes(), 'config': asdict(default_config()),
                'real_api_execution_enabled': False, 'actual_cost_cny': None,
                'future_real_run': {'approval_required': True, 'rate_quote': None, 'budget_cny': None,
                                    'request_ceiling': inventory(cases)['max_model_requests_total']},
                'scope': 'same confirmed text; no OCR calls, no notebook acceptance',
                'arms': {'A': 'fixed keyword retrieval; 1 model request; last 1 dialogue turn',
                         'B': 'main AgentStudyService; <=4 model requests; <=3 tools; last 4 turns'}}
    manifest['plan_id'] = digest(manifest)
    directory.mkdir(parents=True, mode=0o700)
    for name in ('rows', 'previews', 'scopes', 'agent-runs'):
        (directory / name).mkdir(mode=0o700)
    write_json(directory / 'manifest.json', manifest)
    scores = {'schema_version': 1, 'plan_id': manifest['plan_id'],
              'label': '未评分；格式校验与离线手写响应不代表数学或教学质量', 'rows': []}
    for row in rows:
        case = next(c for c in cases if c['case_id'] == row['case_id'])
        turn = case['turns'][row['turn_index']]
        # A later request depends on actual earlier replies. Do not inject synthetic history here.
        context = model_context(case, turn, task_for_turn(None, case, turn), row['arm'])
        write_json(directory / 'previews' / (row['row_id'] + '.json'),
                   {'row_id': row['row_id'], 'context_skeleton': context,
                    'dynamic_dependencies': ['actual prior replies', 'actual tool returns', 'selected next step']
                    if row['turn_index'] else ['actual tool returns']})
        scores['rows'].append({**row, 'reference': case['human_only']['reference'],
            'checks': case['human_only']['checks'] + [case['human_only']['turn_checks'][row['turn_index']]],
            'reviewer': None, 'reviewer_role': None, 'scores': dict.fromkeys(DIMENSIONS),
            'unsupported_inference': None, 'evidence': None, 'disagreement': None})
    write_json(directory / 'scores.json', scores)
    return manifest


def verify(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise AuditError('Symlink run directory is not allowed.')
    manifest = read_json(directory / 'manifest.json')
    unsigned = {k: v for k, v in manifest.items() if k != 'plan_id'}
    if manifest.get('plan_id') != digest(unsigned) or manifest.get('suite') != VERSION:
        raise AuditError('Frozen plan changed.')
    if (manifest['code']['source_hashes'] != source_snapshot()['source_hashes']
            or manifest['assets'] != asset_hashes() or manifest['config'] != asdict(default_config())):
        raise AuditError('Code, cases, rubric, notes or configuration changed; do not reuse this plan.')
    return manifest


class EvaluationScope(ToolScope):
    def __init__(self, *args, fault=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = fault

    def call(self, name, arguments, timeout):
        if name == self.fault:
            raise AgentError('tool_failed')
        return super().call(name, arguments, timeout)


def seed_scope(directory, case, at):
    """Synthetic histories in a NEW isolated folder, never daily student records."""
    directory.mkdir()
    notes = directory / 'notes'
    notes.mkdir()
    for path in sorted([*ROOT.glob('notes/*.json'), *ASSETS.glob('notes/*.json')]):
        (notes / path.name).write_bytes(path.read_bytes())
    book = Notebook(directory / 'notebook')
    if case['history_enabled']:
        rules = {}
        topic = case['history_topic']
        prior_question, prior_work = {
            '通分': ('计算 1/3+1/6。', '1/3+1/6=2/9'),
            '一元一次方程': ('解方程 2x+4=10。', '2x=10+4\nx=7'),
            '单价': ('2 支同样的笔共 6 元，4 支多少元？', '6÷2=3\n3×4=12'),
            '二元一次方程组': ('已知 a+b=10，a-b=2，求 a、b。', '2a=12\na=6\nb=4'),
        }[topic]
        for n, (label, old, enabled) in enumerate([
                ('related', False, True), ('unrelated', False, True),
                ('old', True, True), ('disabled', False, False)]):
            entry_id = digest([case['case_id'], label])[:32]
            entry = make_entry(entry_id, question=prior_question if n != 1 else '同弧所对圆心角为80度，圆周角多少度？',
                               level='初中' if '方程' in topic else '小学', my_work=prior_work if n != 1 else '80÷2=40',
                               topic=topic if n != 1 else '圆周角')
            created = datetime.fromisoformat(at) - timedelta(days=365 if old else 1)
            entry['created_at'] = entry['updated_at'] = created.isoformat()
            book.save_new(entry)
            rules[entry_id] = {'enabled': enabled, 'include_model': True}
        LocalPreferences(directory, history=True).save(0, rules)
    return EvaluationScope(book.directory, notes_dir=notes, history_enabled=case['history_enabled'], fault=case['fault'])


class RecordingLocalTransport:
    """Reserve and persist exact request before HTTP; no remote endpoint or resume path."""
    def __init__(self, server, directory, row, maximum):
        self.base = PhotoTransport(server.chat_url)
        if self.base.kind != 'local_http_test':
            raise AuditError('This rehearsal only permits a loopback server.')
        self.kind, self.endpoint = self.base.kind, self.base.endpoint
        self.directory, self.row, self.maximum = directory, row, maximum

    def send(self, payload, key, timeout, record):
        if key != 'local-test-key' or len(self.row['requests']) >= self.maximum:
            raise AuditError('Local request cap or credential boundary violated.')
        if key in json.dumps(payload, ensure_ascii=False):
            raise AuditError('Credential echo in request.')
        item = {'payload': deepcopy(payload), 'request_hash': digest(payload), 'status': 'reserved'}
        self.row['requests'].append(item)
        write_json(self.directory / 'rows' / (self.row['row_id'] + '.json'), self.row)
        try:
            return self.base.send(payload, key, timeout, record)
        finally:
            item.update(status='returned' if record.get('http_status') else 'unknown',
                        attempted_requests=record.get('attempted_requests', 0), http_status=record.get('http_status'))
            write_json(self.directory / 'rows' / (self.row['row_id'] + '.json'), self.row)


def handwritten_response(context, operation, case, arm, payload):
    """Transport fixtures, deliberately not a solver. Never used for quality scoring."""
    envelope = {'object': 'chat.completion', 'model': 'local-handwritten-not-a-model',
                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': ''}}]}
    choice = envelope['choices'][0]
    if arm == 'B' and not any(m['role'] == 'tool' for m in payload['messages']):
        queries = rule_queries(context, case['history_enabled'])
        if 'unauthorized_request' in case['tags']:
            queries = [('save_notebook', {})]  # Intentional adversarial response, not a user action.
        if queries:
            choice['finish_reason'] = 'tool_calls'
            choice['message'].update(content=None, tool_calls=[
                {'id': f'fixture_{i}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
                for i, (name, args) in enumerate(queries)])
            return envelope
    if operation == 'coach':
        result = {'schema_version': 1, 'status': 'explained',
                  'reply': '本机手写演练：请先检查题目中的已知量和所求量。此文字不是模型教学结果。',
                  'next_step': '写出一个可核对的中间步骤。', 'clarification': ''}
    else:
        kind = context['student_work_kind']
        result = {'schema_version': 2, 'status': 'needs_clarification', 'topic': '本机协议演练',
                  'summary': '固定手写响应只验证流程，不评价本题。', 'steps': [], 'answer': '',
                  'student_review': {'work_kind': kind, 'verdict': 'not_provided' if kind == 'none' else 'uncertain',
                                     'observed_approach': '', 'answer_feedback':
                                     ANSWER_ONLY_FEEDBACK['uncertain'] if kind == 'answer_only' else '', 'comparisons': []},
                  'knowledge_points': [], 'diagnosis': [], 'takeaway': '',
                  'next_practice': '本机演练不作教学判断。', 'clarification': '本机手写占位，等待以后真实评估。'}
    choice['message']['content'] = json.dumps({'result': result, 'citations': []}, ensure_ascii=False)
    return envelope


def run_local(directory):
    directory = Path(directory)
    manifest = verify(directory)
    if datetime.fromisoformat(manifest['created_at']).date() != datetime.now(timezone.utc).date():
        raise AuditError('Create a new preview for today so history age filtering is reproducible.')
    # Exclusive creation prevents a second runner and any retry after crash/failure.
    with (directory / 'execution.json').open('x') as stream:
        json.dump({'mode': 'local_http_test', 'started_at': stamp(), 'real_api_calls': 0}, stream)
    cases = {c['case_id']: c for c in manifest['cases']}
    conversations = {}
    with LocalModelServer() as server:
        for spec in manifest['rows']:
            case, arm = cases[spec['case_id']], spec['arm']
            key = (case['case_id'], arm)
            if key not in conversations:
                conversations[key] = {'task': None, 'previous': None, 'failed': False,
                    'scope': seed_scope(directory / 'scopes' / (case['case_id'] + '-' + arm), case, manifest['created_at'])}
            conversation = conversations[key]
            row = {**spec, 'mode': 'local_http_test', 'status': 'pending', 'requests': [], 'trace': None,
                   'result': None, 'error_code': None, 'elapsed_ms': None, 'human_quality_scores': None,
                   'actual_cost_cny': None, 'notebook_saves': 0, 'synthetic_injection': case['fault'] or
                   ('unauthorized_tool_response' if arm == 'B' and 'unauthorized_request' in case['tags'] else None)}
            path = directory / 'rows' / (spec['row_id'] + '.json')
            if conversation['failed']:
                row['status'] = 'blocked'
                write_json(path, row)
                continue
            turn = case['turns'][spec['turn_index']]
            task = task_for_turn(conversation['task'], case, turn)
            if turn['select_previous_next'] and conversation['previous'] and conversation['previous'].get('next_step'):
                select_next(task, conversation['previous'])
            context = model_context(case, turn, task, arm)
            row.update(status='running', input_context=context)
            write_json(path, row)
            server.body = lambda payload: handwritten_response(context, turn['operation'], case, arm, payload)
            transport = RecordingLocalTransport(server, directory, row, 1 if arm == 'A' else 4)
            service = (FixedWorkflowService(default_config(), 'local-test-key', scope=conversation['scope'], transport=transport)
                       if arm == 'A' else AgentStudyService(default_config(), 'local-test-key', scope=conversation['scope'],
                           audit_dir=directory / 'agent-runs', run_id=digest([manifest['plan_id'], spec['row_id']])[:32],
                           transport=transport, limits=Limits()))
            began = time.monotonic()
            try:
                result = service.call(turn['operation'], ANALYSIS_PROMPT if turn['operation'] == 'analyze' else COACH_PROMPT, context)
                row.update(status='completed', result=result)
                reply = result['summary'] if turn['operation'] == 'analyze' else result['reply']
                remember(task, spec['row_id'], turn['request'], reply, 'local fixture, not real model')
                conversation.update(task=task, previous=result)
            except (ValueError, OSError) as exc:
                row.update(status='failed', error_code=getattr(exc, 'code', 'invalid_content'))
                conversation['failed'] = True
            finally:
                row.update(trace=service.last_run, elapsed_ms=round((time.monotonic() - began) * 1000, 2))
                write_json(path, row)
    # Local cases are independent; injected failures end their conversation, not the entire rehearsal.
    return report(directory)


def report(directory):
    directory = Path(directory)
    manifest = read_json(directory / 'manifest.json')
    if manifest['plan_id'] != digest({k: v for k, v in manifest.items() if k != 'plan_id'}):
        raise AuditError('Plan hash mismatch.')
    output = {'plan_id': manifest['plan_id'], 'mode': 'local_http_test' if (directory / 'execution.json').exists() else 'preview',
              'inventory': manifest['inventory'], 'real_api_calls': 0, 'quality_scores': None,
              'actual_cost_cny': None, 'arms': {}}
    for arm in ('A', 'B'):
        planned = [r for r in manifest['rows'] if r['arm'] == arm]
        stats = {'planned': len(planned), 'completed': 0, 'failed': 0, 'blocked': 0, 'running': 0, 'pending': 0,
                 'reserved_requests': 0, 'http_attempts': 0, 'tool_attempts': 0, 'elapsed_ms': 0,
                 'usage_recorded_requests': 0, 'reported_tokens': 0, 'injected_failure_rows': 0}
        for spec in planned:
            path = directory / 'rows' / (spec['row_id'] + '.json')
            if not path.exists():
                stats['pending'] += 1
                continue
            row = read_json(path)
            if (any(row.get(k) != v for k, v in spec.items()) or row.get('mode') != 'local_http_test'
                    or row.get('status') not in ('completed', 'failed', 'blocked', 'running', 'pending')
                    or any(r['request_hash'] != digest(r['payload']) for r in row['requests'])):
                raise AuditError('Execution row identity, mode or request hash changed.')
            stats[row['status']] += 1
            stats['reserved_requests'] += len(row['requests'])
            stats['http_attempts'] += sum(r.get('attempted_requests', 0) for r in row['requests'])
            trace = row['trace'] or {}
            stats['tool_attempts'] += len(trace.get('tool_calls', []))
            stats['elapsed_ms'] += row['elapsed_ms'] or 0
            for call in trace.get('model_calls', []):
                if call.get('usage'):
                    stats['usage_recorded_requests'] += 1
                    stats['reported_tokens'] += call['usage']['total_tokens']
            stats['injected_failure_rows'] += int(bool(row['synthetic_injection']) and row['status'] == 'failed')
        stats['elapsed_ms'] = round(stats['elapsed_ms'], 2)
        stats['token_usage_complete'] = bool(stats['reserved_requests']) and stats['usage_recorded_requests'] == stats['reserved_requests']
        if not stats['usage_recorded_requests']:
            stats['reported_tokens'] = None
        output['arms'][arm] = stats
    write_json(directory / 'status.json', output)
    lines = ['# A/B 离线评估准备', '', '本机手写响应只验证传输与审计；没有真实模型成绩。',
             '人工参考只在 scores.json 中，实际每次请求与资料在 rows/ 中。未知费用/评分保持 null。', '',
             '| 方案 | 计划回合 | 完成 | 失败 | 阻塞 | 待运行 | HTTP | 工具 |', '|---|---:|---:|---:|---:|---:|---:|---:|']
    for arm, stats in output['arms'].items():
        lines.append('| ' + ' | '.join(str(v) for v in [arm, stats['planned'], stats['completed'], stats['failed'],
                      stats['blocked'], stats['pending'], stats['http_attempts'], stats['tool_attempts']]) + ' |')
    lines += ['', '失败也保留在分母内；预设故障不是供应商故障。空白评分不是0分；完成不代表教学正确。', '',
              '| 回合 | 记录 |', '|---|---|']
    lines += [f"| {r['row_id']} | [输入、回复、资料与调用](rows/{r['row_id']}.json) |" for r in manifest['rows']]
    (directory / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    return output
