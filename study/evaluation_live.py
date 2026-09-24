"""Development-only A/B ledger: freeze, reserve before HTTP, stop on failure, never resume."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
import hashlib
import json
from pathlib import Path
import re
import time

from model_api import ModelConfig
from study.agent import AgentStudyService, Limits
from study.context import COACH_PROMPT, remember, select_next
from study.evaluation import DIMENSIONS, EvaluationScope, asset_hashes, seed_scope
from study.evaluation_baseline import FixedWorkflowService
from study.evaluation_cases import ASSETS, inventory, model_context, selected_cases, task_for_turn
from study.run_audit import AuditError, RunAudit, digest, read_json, redact, stamp, write_json
from study.service import ANALYSIS_PROMPT, PhotoTransport, StudyService
from study.smoke import default_config, source_snapshot

SUITE = 'agent-ab-development-live-v1'
DEFAULT_CASES = ('e03', 'e12')
MAX_REQUEST_BYTES = 64_000
PROTOCOL_TOKENS = 1024
MAX_PLAN_AGE = timedelta(hours=24)


class LedgerStop(AuditError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def rows_for(cases):
    return [{'row_id': f"{c['case_id']}-{a}-t{n + 1:02d}", 'case_id': c['case_id'], 'arm': a, 'turn_index': n}
            for i, c in enumerate(cases) for a in (('A', 'B') if i % 2 == 0 else ('B', 'A'))
            for n in range(len(c['turns']))]


def money(value):
    if type(value) not in (str, int, Decimal):
        raise AuditError('金额必须是十进制字符串，不能是浮点数或布尔值。')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise AuditError('金额格式无效。') from None
    if not result.is_finite() or result <= 0 or result > 1000:
        raise AuditError('金额必须大于0且不超过1000元。')
    return result


def estimate(input_tokens, output_tokens, pricing):
    value = (Decimal(input_tokens) * money(pricing['peak_input_miss_per_million']) +
             Decimal(output_tokens) * money(pricing['peak_output_per_million'])) / 1_000_000
    return value.quantize(Decimal('0.000001'), rounding=ROUND_UP)


def checked_pricing(config, today=None, *, require_fresh=True):
    price = read_json(ASSETS / 'pricing.json')
    age = (today or datetime.now(timezone.utc).date()) - date.fromisoformat(price['checked_on'])
    if (price['schema_version'] != 1 or price['model'] != config.model or price['currency'] != 'CNY'
            or (require_fresh and not timedelta(0) <= age <= timedelta(days=price['valid_days']))):
        raise AuditError('价格快照已过期或与模型不符，请重新核对官方价格并冻结新计划。')
    money(price['peak_input_miss_per_million']); money(price['peak_output_per_million'])
    return price


def input_hashes(directory):
    root = Path(directory) / 'scopes'
    hashes = {}
    if root.is_symlink():
        raise AuditError('资料目录不能是符号链接。')
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise AuditError('冻结资料包含异常文件。')
        if path.is_file():
            if path.stat().st_size > 64 * 1024:
                raise AuditError('资料文件超过上限。')
            hashes[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def create(directory, *, mode='real_api', case_ids=DEFAULT_CASES, budget_cny='2'):
    if mode not in ('real_api', 'local_http_test'):
        raise AuditError('执行模式无效。')
    cases_by_id = {c['case_id']: c for c in selected_cases('development')}
    if (not case_ids or len(set(case_ids)) != len(case_ids)
            or any(c not in cases_by_id for c in case_ids)):
        raise AuditError('只支持明确选择的开发集用例；重复、未知或保留集编号不能运行。')
    cases = [cases_by_id[c] for c in sorted(case_ids)]
    config = default_config()
    price = checked_pricing(config, require_fresh=mode == 'real_api')
    maximum = inventory(cases)['max_model_requests_total']
    per_request = estimate(MAX_REQUEST_BYTES + PROTOCOL_TOKENS, config.max_output_tokens, price)
    budget = money(budget_cny)
    if maximum * per_request > budget:
        raise AuditError('预留预算低于完整范围的保守估算；请缩小范围或明确提高新计划预算。')
    rows = rows_for(cases)
    directory = Path(directory).absolute()
    if directory.exists() or directory.is_symlink():
        raise AuditError('输出目录已存在，不覆盖。')
    directory.mkdir(parents=True, mode=0o700)
    for name in ('rows', 'previews', 'scopes', 'agent-runs'):
        (directory / name).mkdir(mode=0o700)
    created_at = stamp()
    for case in cases:
        for arm in ('A', 'B'):
            seed_scope(directory / 'scopes' / (case['case_id'] + '-' + arm), case, created_at)
    manifest = {'schema_version': 1, 'suite': SUITE, 'created_at': created_at, 'split': 'development',
        'execution_mode': mode, 'cases': cases, 'rows': rows, 'inventory': inventory(cases),
        'code': source_snapshot(), 'assets': asset_hashes(), 'config': asdict(config), 'pricing': price,
        'input_hashes': input_hashes(directory), 'max_model_requests': maximum,
        'max_request_bytes': MAX_REQUEST_BYTES, 'protocol_token_allowance': PROTOCOL_TOKENS,
        'budget_cny': str(budget), 'maximum_reserved_estimate_cny': str(maximum * per_request),
        'automatic_saving': False, 'human_scores': None,
        'arms': {'A': 'same fixed workflow; 1 model call; last 1 turn',
                 'B': 'same main Agent service; <=4 model calls; <=3 tools; last 4 turns'},
        'limits': asdict(Limits()), 'scope': 'confirmed text only; no OCR; no automatic save; no held-out cases'}
    manifest['plan_id'] = digest(manifest)
    write_json(directory / 'manifest.json', manifest)
    sheet = {'schema_version': 1, 'plan_id': manifest['plan_id'], 'label': '人工评分；空白不是0分或通过', 'rows': []}
    for row in rows:
        case = cases_by_id[row['case_id']]
        turn = case['turns'][row['turn_index']]
        context = model_context(case, turn, task_for_turn(None, case, turn), row['arm'])
        write_json(directory / 'previews' / (row['row_id'] + '.json'),
                   {'context_skeleton': context, 'dynamic_dependencies': 'actual prior replies and tool results; no fabricated history'})
        sheet['rows'].append({**row, 'reference': case['human_only']['reference'],
            'checks': case['human_only']['checks'] + [case['human_only']['turn_checks'][row['turn_index']]],
            'reviewer': None, 'reviewer_role': None, 'scores': dict.fromkeys(DIMENSIONS),
            'unsupported_inference': None, 'evidence': None, 'disagreement': None})
    write_json(directory / 'scores.json', sheet)
    plan = EvaluationPlan(directory)
    lines = ['# 开发集 A/B 冻结计划', '', f"计划编号：`{manifest['plan_id']}`", f"代码：`{manifest['code']['commit']}`", '',
        f"用例：{', '.join(c['case_id'] for c in cases)}；两版共{len(rows)}回合，累计最多{maximum}次模型请求。",
        f"预留{budget}元；全范围保守估算{maximum * per_request}元；尚未批准或调用。",
        f"价格核对：{price['checked_on']}，[DeepSeek 官方价格]({price['source']})。按高峰缓存未命中输入{price['peak_input_miss_per_million']}元/百万、输出{price['peak_output_per_million']}元/百万估算。",
        '输入以每请求最多64000 UTF-8字节近似token，并留1024协议余量；输出最多4096 token。不是实际tokenizer或供应商金额封顶。',
        '请求前持久预留次数和估算金额；失败或中断整份停止，不续跑、不回收未知请求额度、不自动收藏。',
        '只使用相同核对文本与隔离合成资料；参考答案只在人工表中。保留集关闭，人工评分保持空白。',
        '本计划24小时内有效，历史日期跨UTC日需重新冻结。代码、价格或输入变化均拒绝旧计划；新计划需重新批准。', '']
    for case in cases:
        lines += [f"## {case['case_id']} · {case['title']}", '', case['turns'][0]['question'], '',
                  '学生原作答：' + (case['turns'][0]['student_work'] or '未提供'),
                  '本轮要求：' + case['turns'][0]['request'], '']
    (directory / 'PLAN.md').write_text('\n'.join(lines) + '\n')
    refresh(plan)
    return plan


class EvaluationPlan(RunAudit):
    def __init__(self, directory):
        super().__init__(directory)
        if self.manifest.get('suite') != SUITE:
            raise AuditError('本入口不能升级旧版离线计划或其他真实计划。')
        for name in ('rows', 'agent-runs', 'scopes'):
            if (self.directory / name).is_symlink():
                raise AuditError('运行子目录不能是符号链接。')
        self.cases = {c['case_id']: c for c in self.manifest['cases']}

    def path(self, row_id):
        if type(row_id) is not str or not re.fullmatch(r'e\d{2}-[AB]-t\d{2}', row_id) or row_id not in self.rows:
            raise AuditError('回合编号无效。')
        return self.directory / 'rows' / (row_id + '.json')

    def row(self, row_id):
        row = super().row(row_id)
        if row:
            if row.get('_audit_hash') != digest({k: v for k, v in row.items() if k != '_audit_hash'}):
                raise AuditError('回合记录完整性检查失败。')
            spec = self.rows[row_id]
            if (any(row.get(k) != v for k, v in spec.items()) or row['mode'] != self.manifest['execution_mode']
                    or row['status'] not in ('running', 'completed', 'failed', 'blocked')):
                raise AuditError('回合记录与冻结范围不符。')
            for item in row['requests']:
                if item['request_hash'] != digest(item['payload']):
                    raise AuditError('已记录请求发生变化。')
            if row['result'] is not None and row['result_hash'] != digest(row['result']):
                raise AuditError('已记录回复发生变化。')
        return row

    def put(self, row_id, value):
        value = deepcopy(value)
        value['_audit_hash'] = digest({k: v for k, v in value.items() if k != '_audit_hash'})
        super().put(row_id, value)

    def scope(self, case_id, arm):
        folder = self.directory / 'scopes' / (case_id + '-' + arm)
        case = self.cases[case_id]
        return EvaluationScope(folder / 'notebook', notes_dir=folder / 'notes',
                               history_enabled=case['history_enabled'], fault=case['fault'])

    def status(self):
        return status(self)


def verify(plan, *, live=False):
    manifest, current = plan.manifest, source_snapshot()
    if read_json(plan.directory / 'manifest.json') != manifest:
        raise AuditError('冻结计划已改变。')
    expected_cases = [c for c in selected_cases('development') if c['case_id'] in plan.cases]
    if (manifest['cases'] != expected_cases or manifest['rows'] != rows_for(expected_cases)
            or manifest['inventory'] != inventory(expected_cases)
            or manifest['max_model_requests'] != inventory(expected_cases)['max_model_requests_total']
            or manifest['max_request_bytes'] != MAX_REQUEST_BYTES
            or manifest['protocol_token_allowance'] != PROTOCOL_TOKENS or manifest['limits'] != asdict(Limits())):
        raise AuditError('冻结范围或上限不符合当前开发集协议。')
    if (manifest['code'] != current or manifest['assets'] != asset_hashes()
            or manifest['config'] != asdict(default_config()) or manifest['pricing'] != checked_pricing(default_config(), require_fresh=live)
            or manifest['input_hashes'] != input_hashes(plan.directory)):
        raise AuditError('代码、价格、配置或资料已变化，请勿执行旧计划。')
    now = datetime.now(timezone.utc)
    created = datetime.fromisoformat(manifest['created_at'])
    if not timedelta(0) <= now - created <= MAX_PLAN_AGE or created.date() != now.date():
        raise AuditError('计划已过期或历史时效跨日，请重新冻结并确认范围。')
    if live and (not current['commit'] or current['dirty']):
        raise AuditError('真实执行必须来自已提交且干净的冻结版本。')


def authorize(plan, confirm_plan, max_requests, budget_cny):
    if (confirm_plan != plan.manifest['plan_id'] or type(max_requests) is not int
            or max_requests != plan.manifest['max_model_requests'] or budget_cny is None
            or money(budget_cny) != money(plan.manifest['budget_cny'])):
        raise AuditError('必须明确确认当前完整计划编号、最大请求数与这份计划的预留预算。')


def preflight(plan, confirm_plan=None, max_requests=None, budget_cny=None):
    if plan.manifest['execution_mode'] != 'real_api':
        raise AuditError('本机计划不能升级为真实调用。')
    verify(plan, live=True)
    authorize(plan, confirm_plan, max_requests, budget_cny)
    state = status(plan)
    if (plan.directory / 'execution.json').exists() and state['execution_status'] != 'completed':
        raise AuditError('已有失败、中断或未完成执行，不自动续跑或重试。')
    return state


def extract_usage(envelope):
    usage = envelope.get('usage') if type(envelope) is dict else None
    if type(usage) is dict:
        counts = [usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if all(type(n) is int and n >= 0 for n in counts) and counts[0] + counts[1] == counts[2]:
            return dict(zip(('input_tokens', 'output_tokens', 'total_tokens'), counts))
    return None


class CountedTransport:
    def __init__(self, plan, row_id, base, expected_context):
        self.plan, self.row_id, self.base, self.expected_context = plan, row_id, base, expected_context
        self.kind, self.endpoint = base.kind, base.endpoint
        if self.kind != plan.manifest['execution_mode']:
            raise AuditError('计划与传输模式不符。')

    def send(self, payload, key, timeout, record):
        plan = self.plan
        plan.require_lock()
        verify(plan, live=self.kind == 'real_api')
        row = plan.row(self.row_id)
        cfg = plan.manifest['config']
        raw = json.dumps(payload, ensure_ascii=False)
        actual_context = json.loads(payload['messages'][1]['content'][0]['text'])
        if (any(actual_context.get(k) != v for k, v in self.expected_context.items())
                or set(actual_context) - set(self.expected_context) - {'attached_images', 'fixed_reference_results'}
                or actual_context.get('attached_images') != []
                or len(payload['messages'][1]['content']) != 1
                or any(payload.get(k) != v for k, v in {'model': cfg['model'], 'max_tokens': cfg['max_output_tokens'],
                    'temperature': cfg['temperature'], 'thinking': {'type': cfg['thinking']}, 'stream': False}.items())
                or payload['response_format'] != {'type': 'json_object'}):
            raise LedgerStop('planned_input_mismatch', '实际请求与本轮核对输入或冻结生成参数不符。')
        if redact(payload, key) != payload:
            raise AuditError('请求包含凭证，未发送或记录。')
        size = len(raw.encode('utf-8'))
        if size > MAX_REQUEST_BYTES:
            raise LedgerStop('request_size_exhausted', '请求超过冻结字节预算；没有截断数学内容或发送。')
        state = status(plan)
        if (state['reserved_model_requests'] >= plan.manifest['max_model_requests']
                or len(row['requests']) >= (1 if row['arm'] == 'A' else 4)):
            raise LedgerStop('request_budget_exhausted', '累计或本回合请求名额已用完。')
        reserve = estimate(size + PROTOCOL_TOKENS, cfg['max_output_tokens'], plan.manifest['pricing'])
        if Decimal(state['reserved_estimate_cny']) + reserve > money(plan.manifest['budget_cny']):
            raise LedgerStop('cost_budget_exhausted', '本次预留会超过估算预算，未发送。')
        index = len(row['requests'])
        item = {'payload': deepcopy(payload), 'request_hash': digest(payload), 'request_bytes': size,
            'status': 'reserved', 'reserved_at': stamp(), 'reserved_estimate_cny': str(reserve),
            'attempted_requests': 0, 'http_status': None, 'completion_unknown': self.kind == 'real_api',
            'usage': None, 'diagnostic_content': None, 'finish_reason': None, 'response_model': None}
        row['requests'].append(item)
        plan.put(self.row_id, row)  # Durable reservation precedes the only send call.
        record['completion_unknown'] = self.kind == 'real_api'
        began = time.monotonic()
        try:
            envelope = self.base.send(payload, key, timeout, record)
            item['usage'] = extract_usage(envelope)
            if type(envelope) is dict:
                response_model = envelope.get('model')
                if type(response_model) is str and re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}', response_model):
                    item['response_model'] = response_model
                choices = envelope.get('choices')
                if type(choices) is list and choices and type(choices[0]) is dict:
                    choice = choices[0]
                    reason = choice.get('finish_reason')
                    item['finish_reason'] = reason[:80] if type(reason) is str else None
                    message = choice.get('message')
                    if type(message) is dict and type(message.get('content')) is str:
                        item['diagnostic_content'] = message['content'][:20000]
            usage = item['usage']
            if redact(envelope, key) != envelope:
                raise LedgerStop('credential_echo', '回复包含疑似凭证，已停止；诊断正文脱敏保存。')
            if usage and (usage['input_tokens'] > size + PROTOCOL_TOKENS or usage['output_tokens'] > cfg['max_output_tokens']):
                raise LedgerStop('usage_exceeds_reservation', '供应商用量超出估算假设，已停止后续请求；本次调用可能已计费。')
            return envelope
        finally:
            item.update(attempted_requests=record.get('attempted_requests', 0), http_status=record.get('http_status'),
                completion_unknown=bool(self.kind == 'real_api' and record.get('http_status') is None),
                status='received' if record.get('http_status') is not None else 'unknown',
                elapsed_ms=round((time.monotonic() - began) * 1000, 2))
            record['completion_unknown'] = item['completion_unknown']
            row = plan.row(self.row_id)
            row['requests'][index] = redact(item, key)  # Never record reasoning_content or the credential.
            plan.put(self.row_id, row)


def execute(plan, *, key='', confirm_plan=None, max_requests=None, budget_cny=None, server=None):
    live = plan.manifest['execution_mode'] == 'real_api'
    if live:
        if server is not None:
            raise AuditError('真实计划不能注入本机回复。')
        preflight(plan, confirm_plan, max_requests, budget_cny)
        StudyService(ModelConfig(**plan.manifest['config']), key)  # Validate before approval/reservation writes.
    elif server is None:
        raise AuditError('本机演练必须明确提供本机服务器。')
    else:
        verify(plan)
        key = 'local-test-key'
    with plan.locked():
        execution_path = plan.directory / 'execution.json'
        if execution_path.exists():
            if status(plan)['execution_status'] == 'completed':
                return status(plan)
            raise AuditError('不续跑已有执行，失败或中断请先检查报告。')
        if any(plan.row(r) is not None for r in plan.rows):
            raise AuditError('已有回合记录，不能重复发送。')
        execution = {'plan_id': plan.manifest['plan_id'], 'mode': plan.manifest['execution_mode'],
                     'status': 'running', 'started_at': stamp(), 'finished_at': None, 'stop_reason': None}
        if live:
            write_json(plan.directory / 'approval.json', {'plan_id': confirm_plan, 'max_model_requests': max_requests,
                       'budget_cny': str(money(budget_cny)), 'at': stamp(), 'actor': 'explicit_cli_confirmation'})
        write_json(execution_path, execution)
        conversations = {}
        try:
            for row_id, spec in plan.rows.items():
                case, arm = plan.cases[spec['case_id']], spec['arm']
                turn = case['turns'][spec['turn_index']]
                conversation = conversations.setdefault((case['case_id'], arm), {'task': None, 'previous': None})
                task = task_for_turn(conversation['task'], case, turn)
                row = {**spec, 'plan_id': plan.manifest['plan_id'], 'mode': plan.manifest['execution_mode'],
                       'status': 'running', 'requests': [], 'result': None, 'result_hash': None, 'trace': None,
                       'error_code': None, 'elapsed_ms': None, 'human_quality_scores': None, 'notebook_saves': 0}
                plan.put(row_id, row)
                service = None
                began = time.monotonic()
                try:
                    verify(plan, live=live)
                    if turn['select_previous_next']:
                        if not conversation['previous'] or not conversation['previous'].get('next_step'):
                            raise LedgerStop('missing_selected_next_step', '上一回合没有可选择的任务，不能伪造选择后继续。')
                        select_next(task, conversation['previous'])
                    context = model_context(case, turn, task, arm)
                    row.update(input_context=context, task_before=deepcopy(task))
                    plan.put(row_id, row)
                    transport = CountedTransport(plan, row_id, PhotoTransport() if live else PhotoTransport(server.chat_url), context)
                    scope = plan.scope(case['case_id'], arm)
                    service = (FixedWorkflowService(default_config(), key, scope=scope, transport=transport)
                               if arm == 'A' else AgentStudyService(default_config(), key, scope=scope,
                                   audit_dir=plan.directory / 'agent-runs', transport=transport,
                                   run_id=digest([plan.manifest['plan_id'], row_id])[:32], limits=Limits()))
                    result = service.call(turn['operation'], ANALYSIS_PROMPT if turn['operation'] == 'analyze' else COACH_PROMPT, context)
                    row = plan.row(row_id)
                    row.update(status='completed', result=result, result_hash=digest(result))
                    remember(task, row_id, turn['request'], result['summary'] if turn['operation'] == 'analyze' else result['reply'],
                             'real model, ungraded' if live else 'local HTTP fixture')
                    conversation.update(task=task, previous=result)
                    row['task_after'] = deepcopy(task)
                except (ValueError, OSError) as exc:
                    row = plan.row(row_id)
                    row.update(status='failed', error_code=getattr(exc, 'code', 'evaluation_stopped'),
                               error_message=redact(str(exc), key)[:500])
                    execution.update(status='stopped', stop_reason=row['error_code'])
                finally:
                    # An interrupt leaves the row running; its reserved request cannot be replayed.
                    row['requests'] = plan.row(row_id)['requests']
                    row.update(trace=service.last_run if service else None,
                               elapsed_ms=round((time.monotonic() - began) * 1000, 2))
                    plan.put(row_id, redact(row, key))
                if row['status'] == 'failed':
                    break
            else:
                execution['status'] = 'completed'
        finally:
            if execution['status'] != 'running':
                execution['finished_at'] = stamp()
            write_json(execution_path, execution)
            refresh(plan)
    return status(plan)


def status(plan):
    execution_path = plan.directory / 'execution.json'
    execution = read_json(execution_path) if execution_path.exists() else None
    if execution and (execution['plan_id'] != plan.manifest['plan_id'] or execution['mode'] != plan.manifest['execution_mode']):
        raise AuditError('执行记录与计划不符。')
    live = plan.manifest['execution_mode'] == 'real_api'
    result = {'plan_id': plan.manifest['plan_id'], 'mode': plan.manifest['execution_mode'] if execution else 'preview',
        'target_mode': plan.manifest['execution_mode'], 'execution_status': execution['status'] if execution else 'not_started',
        'inventory': plan.manifest['inventory'], 'max_model_requests': plan.manifest['max_model_requests'],
        'budget_cny': plan.manifest['budget_cny'], 'reserved_model_requests': 0, 'reserved_estimate_cny': '0',
        'known_real_api_attempts': 0, 'real_api_calls_unknown': False, 'actual_cost_cny': None,
        'provider_usage_peak_estimate_cny': None, 'usage_complete': False, 'quality_scores': None,
        'human_scores': None, 'notebook_saves': 0, 'arms': {}, 'failures': []}
    all_requests = []
    for arm in ('A', 'B'):
        specs = [r for r in plan.rows.values() if r['arm'] == arm]
        counts = dict.fromkeys(('completed', 'failed', 'running', 'blocked', 'pending'), 0)
        stats = {'planned': len(specs), 'http_attempts': 0, 'tool_attempts': 0, 'elapsed_ms': 0, 'usage_recorded_requests': 0}
        for spec in specs:
            row = plan.row(spec['row_id'])
            if row is None:
                counts['pending'] += 1
                continue
            counts[row['status']] += 1
            all_requests.extend(row['requests'])
            stats['http_attempts'] += sum(r.get('attempted_requests', 0) for r in row['requests'])
            trace = row['trace'] or {}
            stats['tool_attempts'] += len(trace.get('tool_calls', []))
            stats['elapsed_ms'] += row['elapsed_ms'] or 0
            stats['usage_recorded_requests'] += sum(r['usage'] is not None for r in row['requests'])
            if row['status'] == 'failed':
                result['failures'].append({'row_id': row['row_id'], 'error_code': row['error_code']})
            if live and row['status'] == 'running':
                result['real_api_calls_unknown'] = True
        result['arms'][arm] = {**stats, **counts, 'elapsed_ms': round(stats['elapsed_ms'], 2)}
    result['reserved_model_requests'] = len(all_requests)
    result['reserved_estimate_cny'] = str(sum((Decimal(r['reserved_estimate_cny']) for r in all_requests), Decimal(0)))
    result['known_real_api_attempts'] = sum(r['attempted_requests'] for r in all_requests) if live else 0
    result['real_api_calls_unknown'] |= live and any(r['completion_unknown'] or r['status'] == 'reserved' for r in all_requests)
    usages = [r['usage'] for r in all_requests if r['usage'] is not None]
    result['usage_complete'] = bool(all_requests) and len(usages) == len(all_requests)
    result['usage_totals_reported'] = {k: sum(u[k] for u in usages) for k in ('input_tokens', 'output_tokens', 'total_tokens')} if usages else None
    if usages and live:
        result['provider_usage_peak_estimate_cny'] = str(sum((estimate(u['input_tokens'], u['output_tokens'], plan.manifest['pricing']) for u in usages), Decimal(0)))
    return result


def refresh(plan):
    value = status(plan)
    write_json(plan.directory / 'status.json', value)
    lines = ['# A/B 开发集执行报告', '', f"执行状态：{value['execution_status']}；目标模式：{value['target_mode']}。",
             f"累计预留{value['reserved_model_requests']}次 / 最多{value['max_model_requests']}次；预留估算{value['reserved_estimate_cny']}元 / 预算{value['budget_cny']}元。",
             '实际账单未知；人工分数为空。格式通过不表示数学正确。本机演练不记为真实模型成绩。', '',
             '| 回合 | 状态 | 请求记录 |', '|---|---|---|']
    for row_id in plan.rows:
        row = plan.row(row_id)
        lines.append(f"| {row_id} | {row['status'] if row else 'pending'} | [原始正文、实际资料与账本](rows/{row_id}.json) |")
    (plan.directory / 'REPORT.md').write_text('\n'.join(lines) + '\n')
    return value
