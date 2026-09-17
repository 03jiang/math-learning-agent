"""复用 StudyService 与 Notebook 的可暂停文字验证；不接旧四题参考答案。"""
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import subprocess

from model_api import ModelConfig, load_model_config
from study.corrections import validate_result
from study.diagnosis import validate_analysis
from study.notebook import Notebook, make_entry
from study.output_contract import endpoint, CONTRACT_VERSION
from study.run_audit import AuditError, RunAudit, digest, read_json, write_json, stamp
from study.service import (StudyService, PhotoTransport, ANALYSIS_PROMPT, CORRECTION_PROMPT,
                           ANALYSIS_PROMPT_VERSION, CORRECTION_PROMPT_VERSION,
                           analysis_context, correction_context, build_payload)

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / 'evaluation/study_smoke_v1.json'


def source_snapshot():
    paths = sorted({*ROOT.glob('*.py'), *ROOT.glob('study/*.py'), *ROOT.glob('tools/*.py'),
                    CASES, ROOT / 'evaluation/study_smoke_local_responses.json',
                    ROOT / 'requirements-lock.txt', ROOT / 'model_config.deepseek.example.json'})
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                         stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT,
                                            stderr=subprocess.DEVNULL, text=True, timeout=3).strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {'commit': commit, 'dirty': dirty, 'source_hashes': hashes}


def default_config():
    # 与当前照片 UI 的覆盖参数一致。
    return replace(load_model_config(ROOT / 'model_config.deepseek.example.json'),
                   max_output_tokens=4096, timeout_seconds=60)


def make_rows(config, output_mode='json_object'):
    data = read_json(CASES)
    if data['schema_version'] != 1 or len(data['cases']) != 5:
        raise AuditError('文字验证必须有 5 道已冻结题目。')
    rows, corrections = [], []
    for i, case in enumerate(data['cases'], 1):
        context = analysis_context(case['question'], case['level'], case['student_work'], work_kind=case['work_kind'])
        row_id = f'b{i:02d}'
        rows.append({'row_id': row_id, 'case_id': case['id'], 'title': case['title'],
                     'operation': 'analyze', 'depends_on': None, 'context': context,
                     'expected_behavior': case['expected_behavior'], 'human_reference': case['human_reference'],
                     'payload': build_payload(config, ANALYSIS_PROMPT, context, output_mode=output_mode)})
        if case['correction']:
            correction = case['correction']
            next_context = analysis_context(case['question'], case['level'], correction['student_work'],
                                            work_kind=correction['work_kind'])
            corrections.append({'case_id': case['id'], 'title': case['title'] + '／再次作答',
                'operation': 'reanalyze', 'depends_on': row_id, 'context': next_context,
                'expected_behavior': correction['expected_behavior'], 'human_reference': correction['human_reference'],
                'payload': None,
                'payload_dependency': '确认保存原分析后，由同一 StudyService 拼入真实 previous_analysis；不预填示例答案'})
    for i, row in enumerate(corrections, 6):
        rows.append({'row_id': f'b{i:02d}', **row})
    if len(rows) != 7:
        raise AuditError('计划必须为 5 次分析和 2 次订正。')
    return rows


def select_rows(rows, row_ids=None):
    if row_ids is None:
        return rows
    known = {row['row_id'] for row in rows}
    if (type(row_ids) is not list or not row_ids
            or any(type(row_id) is not str or row_id not in known for row_id in row_ids)
            or len(row_ids) != len(set(row_ids))):
        raise AuditError('所选请求编号为空、重复或无效。')
    selected = [row for row in rows if row['row_id'] in row_ids]
    if any(row['depends_on'] and row['depends_on'] not in row_ids for row in selected):
        raise AuditError('选择订正请求时必须同时选择其原分析，不能注入另一计划的结果。')
    return selected


def confirmed_source(directory, row_id, mode):
    """只读核对原计划、用户决定及版本 1 存档，不续跑或改写原账本。"""
    source = RunAudit(directory)
    if row_id not in source.rows or source.rows[row_id]['operation'] != 'analyze':
        raise AuditError('复用来源必须是原分析。')
    row = source.row(row_id)
    decision = row.get('decision') if row else None
    if (not row or row['status'] != 'reply_valid' or not decision
            or decision['action'] != 'accept' or decision['saved_version'] != 1
            or not decision['reopen_verified'] or digest(row['result']) != row['result_hash']
            or row['payload'] != source.rows[row_id]['payload']
            or row['request_hash'] != digest(row['payload'])):
        raise AuditError('来源未确认保存，或记录不一致。')
    if mode == 'real_api' and (source.manifest['execution_mode'] != 'real_api'
            or row['mode'] != 'real_api' or decision['actor'] != 'user'
            or row['call']['kind'] != 'real_api' or row['call']['status'] != 'ok'):
        raise AuditError('真实订正只能复用用户确认的真实分析。')
    context = source.rows[row_id]['context']
    validate_analysis(row['result'], student_work=context['student_work'], work_kind=context['student_work_kind'])
    origin = ('DeepSeek / ' + source.manifest['config']['model'])[:100] if row['mode'] == 'real_api' else '本机 HTTP 手写响应（非模型）'
    expected = make_entry(entry_id(source, row_id), question=context['confirmed_question'],
        level=context['school_level'], my_work=context['student_work'], topic=row['result']['topic'],
        analysis=row['result'], analysis_origin=origin)
    expected['created_at'] = expected['updated_at'] = row['finished_at']
    book = notebook(source)
    entry = book.get(decision['entry_id'])
    if entry != expected:
        raise AuditError('来源存档已变更，或与已确认原分析不符。')
    paths = [source.directory / 'manifest.json', source.path(row_id), book.path(entry['id'])]
    return {'directory': str(source.directory), 'plan_id': source.manifest['plan_id'],
        'row_id': row_id, 'entry': entry,
        'artifact_hashes': {p.relative_to(source.directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in paths}}


def reused_correction(rows, directory, row_ids, mode, config, output_mode):
    if type(row_ids) is not list or len(row_ids) != 1:
        raise AuditError('复用存档时必须只选一条订正。')
    selected = [r for r in rows if r['row_id'] == row_ids[0] and r['operation'] == 'reanalyze']
    if len(selected) != 1:
        raise AuditError('复用存档只支持订正请求。')
    row = selected[0]
    source = confirmed_source(directory, row['depends_on'], mode)
    original = next(r for r in rows if r['row_id'] == row['depends_on'])['context']
    entry = source['entry']
    if (entry['question'], entry['level'], entry['my_work']) != (
            original['confirmed_question'], original['school_level'], original['student_work']):
        raise AuditError('来源题目或原作答与本次案例不一致。')
    row = {**row, 'depends_on': None,
        'payload_dependency': '只读复用 parent_source 中用户已确认的原分析；不重发分析请求',
        'payload': build_payload(config, CORRECTION_PROMPT,
            correction_context(entry, row['context']['student_work'], work_kind=row['context']['student_work_kind']),
            output_mode=output_mode, operation='reanalyze')}
    return [row], source


def verify_source(audit):
    frozen = audit.manifest['parent_source']
    current = confirmed_source(frozen['directory'], frozen['row_id'], audit.manifest['execution_mode'])
    if current != frozen:
        raise AuditError('复用来源已变化，未请求或保存；请重新核对。')
    return current['entry']


def create_run(output, *, mode='real_api', config=None, row_ids=None, output_mode='json_object', reuse_from=None):
    if mode not in ('real_api', 'local_http_test'):
        raise AuditError('未知运行模式。')
    config = config or default_config()
    if (config.mode, config.provider, config.api_format) != ('api', 'deepseek', 'chat_completions'):
        raise AuditError('文字验证使用 DeepSeek Chat Completions 配置。')
    provider_endpoint = endpoint(config, output_mode)
    rows = make_rows(config, output_mode)
    selected, source = (reused_correction(rows, reuse_from, row_ids, mode, config, output_mode)
                        if reuse_from is not None else (select_rows(rows, row_ids), None))
    audit = RunAudit.create(output, {'schema_version': 1, 'suite': 'study-text-smoke-v1',
        'execution_mode': mode, 'code': source_snapshot(), 'config': asdict(config),
        'output_mode': output_mode, 'provider_endpoint': provider_endpoint,
        'output_contract': CONTRACT_VERSION if output_mode == 'strict_tool' else 'json-object-v1',
        'prompt_versions': {'analyze': ANALYSIS_PROMPT_VERSION, 'reanalyze': CORRECTION_PROMPT_VERSION},
        'row_selection': [row['row_id'] for row in selected],
        **({'parent_source': source} if source else {}),
        'rows': selected, 'planned_requests': len(selected), 'real_requests_on_preview': 0,
        'cost_estimate_cny': None, 'human_scoring': '未评分'})
    write_json(audit.directory / 'review.json', {'label': '人工检查表；空白为未评，不是 0 分',
        'rows': [{'row_id': r['row_id'], 'expected_behavior': r['expected_behavior'],
                  'human_reference': r['human_reference'], 'reviewer': None,
                  'math_score_0_1_2': None, 'evidence_score_0_1_2': None,
                  'unsupported_inference': None, 'notes': ''} for r in audit.rows.values()]})
    report(audit)
    return audit


def verify_frozen(audit, *, live=False):
    current = source_snapshot()
    if current != audit.manifest['code']:
        raise AuditError('代码或 Git 版本已变化；请新建预览，不续跑旧计划。')
    config = ModelConfig(**audit.manifest['config'])
    output_mode = audit.manifest.get('output_mode', 'json_object')
    expected_contract = CONTRACT_VERSION if output_mode == 'strict_tool' else 'json-object-v1'
    if (audit.manifest.get('provider_endpoint') != endpoint(config, output_mode)
            or audit.manifest.get('output_contract') != expected_contract):
        raise AuditError('返回格式或供应商地址与当前实现不符。')
    if 'parent_source' in audit.manifest:
        selected, source = reused_correction(make_rows(config, output_mode),
            audit.manifest['parent_source']['directory'], audit.manifest.get('row_selection'),
            audit.manifest['execution_mode'], config, output_mode)
        if source != audit.manifest['parent_source']:
            raise AuditError('复用来源已改变。')
    else:
        selected = select_rows(make_rows(config, output_mode), audit.manifest.get('row_selection'))
    if selected != list(audit.rows.values()):
        raise AuditError('案例或请求与当前实现不符。')
    if live and (not current['commit'] or current['dirty']):
        raise AuditError('真实验证需要干净的 Git 提交，请先提交并重新预览。')


def entry_id(audit, row_id):
    return digest([audit.manifest['plan_id'], row_id])[:32]


def notebook(audit):
    path = audit.directory / 'notebook'
    if path.is_symlink():
        raise AuditError('验证存档目录异常。')
    return Notebook(path)


def prepare(audit, row):
    if row['operation'] == 'analyze':
        return None, row['payload']
    if 'parent_source' in audit.manifest:
        return verify_source(audit), row['payload']
    parent = audit.row(row['depends_on'])
    if not parent or not parent['decision'] or parent['decision']['action'] != 'accept':
        return None, None
    previous = notebook(audit).get(entry_id(audit, row['depends_on']))
    if previous['version'] != 1 or previous['analysis'] != parent['result']:
        raise AuditError('对照存档已更新，不能用旧计划订正。')
    context = correction_context(previous, row['context']['student_work'],
                                 work_kind=row['context']['student_work_kind'])
    return previous, build_payload(ModelConfig(**audit.manifest['config']), CORRECTION_PROMPT, context,
        output_mode=audit.manifest.get('output_mode', 'json_object'), operation='reanalyze')


def execute(audit, *, key='', max_requests=None, confirm_plan=None, budget_note=None, server=None):
    """只执行尚未尝试、依赖已确认的行；任一失败或中断后禁止自动续发。"""
    live = audit.manifest['execution_mode'] == 'real_api'
    output_mode = audit.manifest.get('output_mode', 'json_object')
    verify_frozen(audit, live=live)
    if live:
        if server is not None:
            raise AuditError('真实模式不能注入模拟响应。')
        if (type(max_requests) is not int or not 1 <= max_requests <= len(audit.rows)
                or confirm_plan != audit.manifest['plan_id']
                or type(budget_note) is not str or not budget_note.strip() or len(budget_note) > 500):
            raise AuditError('需确认完整计划编号、累计请求上限和费用预算说明。')
        # 仅检查格式；不把凭证写进 approval。
        StudyService(ModelConfig(**audit.manifest['config']), key, output_mode=output_mode)
        if key in budget_note:
            raise AuditError('预算说明不能包含凭证。')
    elif server is None:
        raise AuditError('本机模式需要本机手写响应服务。')
    with audit.locked():
        state = audit.status()
        if state['counts']['failed'] or state['counts']['running']:
            raise AuditError('已有失败或完成情况未知的请求；已停止，不重试或自动跳过。')
        approval = {'plan_id': confirm_plan, 'max_requests': max_requests, 'budget_note': budget_note}
        if live:
            path = audit.directory / 'approval.json'
            if path.exists() and read_json(path) != approval:
                raise AuditError('不能在续跑时提高上限或替换预算说明。')
            if not path.exists():
                write_json(path, approval)
        cap = max_requests if live else len(audit.rows)
        for row_id, planned in audit.rows.items():
            if audit.row(row_id) is not None:
                continue
            if audit.status()['reserved_attempts'] >= cap:
                break
            previous, payload = prepare(audit, planned)
            if payload is None:
                continue
            audit.expected_payload = payload
            config = ModelConfig(**audit.manifest['config'])
            service = StudyService(config, key if live else 'local-test-key',
                None if live else PhotoTransport(server.chat_url.replace('/chat/completions',
                    '/beta/chat/completions') if output_mode == 'strict_tool' else server.chat_url),
                audit=audit, request_id=row_id, output_mode=output_mode)
            context = planned['context']
            try:
                if planned['operation'] == 'analyze':
                    service.analyze(context['confirmed_question'], context['school_level'],
                                    context['student_work'], work_kind=context['student_work_kind'])
                else:
                    service.reanalyze(previous, context['student_work'], work_kind=context['student_work_kind'])
            except (ValueError, OSError):
                # 不输出原始异常正文。审计写失败时 running 保留，下一次也不发送。
                state = report(audit)
                state['execution_error'] = 'stopped; inspect row status before any further action'
                write_json(audit.directory / 'status.json', state)
                return state
    report(audit)
    return audit.status()


def decide(audit, row_id, action, *, actor='user'):
    if action not in ('accept', 'reject') or actor not in ('user', 'local_fixture'):
        raise AuditError('未知决定。')
    if actor == 'local_fixture' and audit.manifest['execution_mode'] != 'local_http_test':
        raise AuditError('真实结果必须由用户决定保存。')
    with audit.locked():
        row = audit.row(row_id)
        if row is None or row['status'] != 'reply_valid':
            raise AuditError('本行没有通过校验的结果。')
        if row['decision']:
            if row['decision']['action'] != action:
                raise AuditError('该结果已有决定，不能反向覆盖。')
            return row['decision']
        if digest(row['result']) != row['result_hash']:
            raise AuditError('分析结果被修改，不能确认。')
        saved = None
        if action == 'accept':
            if datetime.now(timezone.utc) - datetime.fromisoformat(row['finished_at']) > timedelta(hours=24):
                raise AuditError('候选分析超过 24 小时，请重新核对；没有保存。')
            planned = audit.rows[row_id]
            context = planned['context']
            origin = ('DeepSeek / ' + audit.manifest['config']['model'])[:100] if row['mode'] == 'real_api' else '本机 HTTP 手写响应（非模型）'
            book = notebook(audit)
            if planned['operation'] == 'analyze':
                validate_analysis(row['result'], student_work=context['student_work'], work_kind=context['student_work_kind'])
                entry = make_entry(entry_id(audit, row_id), question=context['confirmed_question'],
                    level=context['school_level'], my_work=context['student_work'], topic=row['result']['topic'],
                    analysis=row['result'], analysis_origin=origin)
                # 稳定时间与编号让“写入后、决定日志前”崩溃的重放也保持幂等。
                entry['created_at'] = entry['updated_at'] = row['finished_at']
                book.save_new(entry)
                saved = book.get(entry['id'])
            else:
                initial = verify_source(audit) if 'parent_source' in audit.manifest else None
                if initial is not None and row['payload'] != planned['payload']:
                    raise AuditError('订正请求与冻结预览不符，未保存。')
                parent_id = initial['id'] if initial else entry_id(audit, planned['depends_on'])
                previous_context = json.loads(row['payload']['messages'][1]['content'][0]['text'])
                validate_result(row['result'], previous_work=previous_context['previous_student_work'],
                    previous_analysis=previous_context['previous_analysis'], answer=context['student_work'],
                    work_kind=context['student_work_kind'])
                saved = book.add_correction(parent_id, 1, entry_id(audit, row_id), based_on='original',
                    answer=context['student_work'], work_kind=context['student_work_kind'], result=row['result'],
                    analysis_origin=origin, initial_entry=initial)
            if saved != Notebook(book.directory).get(saved['id']):
                raise AuditError('重新读取与保存结果不一致。')
        row['decision'] = {'action': action, 'actor': actor, 'at': stamp(),
                           'entry_id': saved['id'] if saved else None,
                           'saved_version': saved['version'] if saved else None,
                           'reopen_verified': saved is not None}
        audit.put(row_id, row)
    report(audit)
    return row['decision']


def report(audit):
    state = audit.status()
    write_json(audit.directory / 'status.json', state)
    analyses = sum(row['operation'] == 'analyze' for row in audit.rows.values())
    corrections = len(audit.rows) - analyses
    lines = ['# 当前主流程文字验证', '',
        '**本机手写响应，只验证软件流程。**' if state['mode'] == 'local_http_test' else '**请求预览 / 真实执行账本；以每行状态为准。**',
        '', f"计划编号：`{state['plan_id']}`", '',
        f"返回格式：`{audit.manifest.get('output_mode', 'json_object')}`；地址：`{audit.manifest.get('provider_endpoint', '见冻结配置')}`。",
        f'{analyses} 次分析 + {corrections} 次依赖已确认存档的订正，最多 {len(audit.rows)} 次。预览不请求模型。',
        '费用未知，不等于免费；请求次数是硬上限，预算说明是人工确认记录，不是供应商扣费封顶。',
        '参考答案只用于人工核对，不进入模型请求。review.json 的空值为未评。', '',
        '| 请求 | 题目 | 操作 | 依赖 | 状态 | 保存决定 |', '|---|---|---|---|---|---|']
    for row_id, planned in audit.rows.items():
        row = audit.row(row_id)
        lines.append(f"| {row_id} | {planned['title']} | {planned['operation']} | {planned['depends_on'] or '无'} | "
                     f"{row['status'] if row else 'pending'} | {row['decision']['action'] if row and row['decision'] else '未决定'} |")
    lines += ['', 'manifest.json 保存输入、参数、提示词及代码指纹。responses/ 保存实际请求、脱敏回复和调用记录。',
              'Notebook 仅写入本报告目录的 notebook/；不修改应用中的私人错题本。',
              '订正的 previous_analysis 要等原分析真实返回且用户确认后才可确定；预览不使用固定回复补齐。',
              '失败或完成情况未知会停止整个计划；没有自动重试。恢复读取不等于再次发送请求。', '']
    if 'parent_source' in audit.manifest:
        source = audit.manifest['parent_source']
        lines += [f"复用来源：计划 `{source['plan_id']}` 的 `{source['row_id']}`；原目录只读。",
            'parent_source 冻结来源文件哈希和已确认存档，订正 payload 已完整预览。',
            '只有确认本次订正后，才将原记录与新订正一次写入本计划 notebook；拒绝不创建错题本。',
            '复用不计为本计划的模型调用或新分析保存。原计划中的失败记录保持原样。', '']
    (audit.directory / 'review.md').write_text('\n'.join(lines))
    return state
