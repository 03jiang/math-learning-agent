"""修复校验器后复核已有回复：零模型调用，原失败不变，新候选须另行确认。"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from study.corrections import EVIDENCE_VERSION, validate_result
from study.notebook import Notebook
from study.output_contract import parse_output, output_schema
from study.run_audit import AuditError, RunAudit, digest, read_json, write_json, stamp
from study.smoke import source_snapshot, verify_source


def failed_input(directory, row_id):
    audit = RunAudit(directory)
    row = audit.row(row_id)
    planned = audit.rows[row_id]
    call = row['call'] if row else {}
    if (not row or row['status'] != 'failed' or row['decision'] is not None
            or planned['operation'] != 'reanalyze' or row['result'] is not None
            or call.get('error_code') != 'invalid_content'
            or call.get('validation_issue') != 'correction_current_verdict_mismatch'
            or call.get('http_status') != 200 or call.get('completion_unknown')
            or call.get('status') != 'error' or call.get('attempted_requests') != 1
            or call.get('kind') != audit.manifest['execution_mode']
            or row['mode'] != audit.manifest['execution_mode']
            or audit.manifest.get('output_mode') != 'strict_tool'
            or call.get('output_function') != 'return_math_correction'):
        raise AuditError('只能复核已完整返回、因本次步骤证据校验失败的 strict 订正。')
    if (row['payload'] != planned['payload'] or digest(row['payload']) != row['request_hash']
            or row['request_hash'] != call.get('request_hash')):
        raise AuditError('原请求与冻结计划不一致，不能复核。')
    parent = verify_source(audit)  # 本入口仅复用已有来源链，不接受手填的旧分析。
    context = parse_output(row['payload']['messages'][1]['content'][0]['text'])
    if (context['previous_analysis'] != parent['analysis'] or context['previous_student_work'] != parent['my_work']
            or context['confirmed_question'] != parent['question'] or context['school_level'] != parent['level']
            or context['student_work'] != planned['context']['student_work']
            or context['student_work_kind'] != planned['context']['student_work_kind']):
        raise AuditError('原请求的前后作答与存档不一致。')
    raw = row['diagnostic_content']
    if type(raw) is not str or len(raw) >= 8000:
        raise AuditError('诊断原文可能截断，不能恢复候选。')
    result = parse_output(raw)
    schema = row['payload']['tools'][0]['function']['parameters']
    if schema != output_schema('reanalyze',context=context):
        raise AuditError('原返回格式不属于当前支持的离线复核范围。')
    try:
        Draft202012Validator(schema).validate(result)
    except ValidationError:
        raise AuditError('原回复不符合冻结格式，未生成复核候选。') from None
    validate_result(result, previous_work=context['previous_student_work'],
        previous_analysis=context['previous_analysis'], answer=context['student_work'],
        work_kind=context['student_work_kind'])
    reference = {'directory': str(audit.directory), 'plan_id': audit.manifest['plan_id'], 'row_id': row_id,
        'original_status': 'failed', 'original_issue': call['validation_issue'],
        'mode': row['mode'], 'response_sha256': hashlib.sha256(audit.path(row_id).read_bytes()).hexdigest(),
        'manifest_sha256': hashlib.sha256((audit.directory/'manifest.json').read_bytes()).hexdigest(),
        'raw_sha256': hashlib.sha256(raw.encode()).hexdigest()}
    return reference, parent, context, result, audit


def create_review(source, row_id, output):
    reference, parent, context, result, audit = failed_input(source, row_id)
    code = source_snapshot()
    if reference['mode'] == 'real_api' and (not code['commit'] or code['dirty']):
        raise AuditError('真实回复的复核候选需要干净提交；没有写入候选。')
    document = {'schema_version': 1, 'kind': 'offline_correction_revalidation',
        'created_at': stamp(), 'code': code, 'evidence_version': EVIDENCE_VERSION,
        'source': reference, 'parent_source': audit.manifest['parent_source'],
        'result': result, 'result_hash': digest(result), 'additional_model_calls': 0}
    document['review_id'] = digest(document)
    directory = Path(output).absolute()
    if directory.exists() or directory.is_symlink():
        raise AuditError('复核目录必须是新目录，不覆盖旧记录。')
    directory.mkdir(parents=True, mode=0o700)
    write_json(directory/'candidate.json', document)
    return document


def load_review(directory):
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise AuditError('复核目录异常。')
    document = read_json(directory/'candidate.json')
    if (document['schema_version'] != 1 or document['kind'] != 'offline_correction_revalidation'
            or document['additional_model_calls'] != 0):
        raise AuditError('未知复核候选格式。')
    if document['review_id'] != digest({k:v for k,v in document.items() if k!='review_id'}):
        raise AuditError('复核候选已被修改。')
    if document['code'] != source_snapshot() or document['evidence_version'] != EVIDENCE_VERSION:
        raise AuditError('校验代码已变化，请重新核对；没有保存。')
    reference, parent, context, result, audit = failed_input(document['source']['directory'], document['source']['row_id'])
    if (reference != document['source'] or audit.manifest['parent_source'] != document['parent_source']
            or result != document['result'] or digest(result) != document['result_hash']):
        raise AuditError('复核来源或候选已变化，未保存。')
    return document, parent, context, audit


@contextmanager
def review_lock(directory):
    path = Path(directory)/'.review.lock'
    if path.is_symlink():
        raise AuditError('复核锁异常。')
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuditError('另一进程正在确认本条复核。') from None
        yield


def decide_review(directory, action, *, actor='user'):
    if action not in ('accept','reject') or actor not in ('user','local_fixture'):
        raise AuditError('未知复核决定。')
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise AuditError('复核目录异常。')
    with review_lock(directory):
        document, parent, context, audit = load_review(directory)
        if actor != 'user' and document['source']['mode'] == 'real_api':
            raise AuditError('真实历史回复必须由用户决定。')
        decision_path = directory/'decision.json'
        if decision_path.exists():
            prior = read_json(decision_path)
            if prior['review_id'] != document['review_id'] or prior['action'] != action:
                raise AuditError('此候选已有不同决定，不覆盖。')
            return prior
        saved = None
        if action == 'accept':
            if datetime.now(timezone.utc)-datetime.fromisoformat(document['created_at']) > timedelta(hours=24):
                raise AuditError('复核候选已过期，请重新核对；没有保存。')
            book = Notebook(directory/'notebook')
            if book.directory.is_symlink():
                raise AuditError('保存目录异常。')
            origin = ('DeepSeek / '+audit.manifest['config']['model']+'（历史回复离线复核）')[:100]
            if document['source']['mode'] != 'real_api':
                origin = '本机 HTTP 手写回复离线复核（非模型）'
            saved = book.add_correction(parent['id'],1,digest([document['review_id'],'correction'])[:32],
                based_on='original',answer=context['student_work'],work_kind=context['student_work_kind'],
                result=document['result'],analysis_origin=origin,initial_entry=parent)
            if saved != Notebook(book.directory).get(parent['id']):
                raise AuditError('保存后重新读取不一致。')
        decision = {'review_id':document['review_id'],'action':action,'actor':actor,'at':stamp(),
            'entry_id':saved['id'] if saved else None,'saved_version':saved['version'] if saved else None,
            'reopen_verified':saved is not None,'additional_model_calls':0}
        write_json(decision_path,decision)
    return decision
