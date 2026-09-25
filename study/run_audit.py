"""文字验证的本地运行账本。先记录尝试，再发送；不保存密钥、错误正文或思维链。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from legacy.model_boundary import _unique_object, _reject_constant


class AuditError(ValueError):
    """可以安全展示的本地校验错误；文本不得拼接凭证或服务端正文。"""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise AuditError('运行记录异常；没有覆盖。')
    return json.loads(path.read_text(), object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def write_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise AuditError('拒绝覆盖符号链接。')
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.audit-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def redact(value, secret):
    # 不接收原始 HTTP envelope。即使非法 JSON 也只保留限长、脱敏的 content。
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str):
        value = value.replace(secret, '[credential removed]') if secret else value
        return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})',
                      '[credential removed]', value)
    return value


class RunAudit:
    def __init__(self, directory):
        path = Path(directory).absolute()
        if path.is_symlink() or (path / 'responses').is_symlink():
            raise AuditError('运行目录不能通过符号链接访问。')
        self.directory = path.resolve()
        self.manifest = read_json(self.directory / 'manifest.json')
        unsigned = {k: v for k, v in self.manifest.items() if k != 'plan_id'}
        if self.manifest.get('plan_id') != digest(unsigned):
            raise AuditError('请求计划已改变，不能继续。')
        self.rows = {row['row_id']: row for row in self.manifest['rows']}
        if len(self.rows) != len(self.manifest['rows']):
            raise AuditError('请求编号重复。')
        self._locked = False
        self.expected_payload = None

    @classmethod
    def create(cls, directory, manifest):
        directory = Path(directory).absolute()
        if directory.exists() or directory.is_symlink():
            raise AuditError('输出目录必须是新的普通目录，不覆盖已有报告。')
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'responses').mkdir(mode=0o700)
        manifest = deepcopy(manifest)
        manifest['created_at'] = stamp()
        manifest['plan_id'] = digest(manifest)
        write_json(directory / 'manifest.json', manifest)
        return cls(directory)

    def path(self, row_id):
        if type(row_id) is not str or not re.fullmatch(r'b\d{2}', row_id) or row_id not in self.rows:
            raise AuditError('未知请求编号。')
        return self.directory / 'responses' / (row_id + '.json')

    def row(self, row_id):
        path = self.path(row_id)
        if path.exists():
            row = read_json(path)
            if row.get('row_id') != row_id or row.get('plan_id') != self.manifest['plan_id']:
                raise AuditError('运行记录与计划不符。')
            return row
        return None

    @contextmanager
    def locked(self):
        lock = self.directory / '.run.lock'
        if lock.is_symlink():
            raise AuditError('锁文件异常。')
        with lock.open('a') as stream:
            os.chmod(lock, 0o600)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AuditError('这个运行目录正在使用中。') from None
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False

    def require_lock(self):
        if not self._locked:
            raise AuditError('写入运行记录前必须持有运行锁。')

    def put(self, row_id, value):
        self.require_lock()
        write_json(self.path(row_id), value)

    def begin(self, row_id, operation, payload, secret):
        self.require_lock()
        if self.row(row_id) is not None:
            raise AuditError('本请求已登记，不会重复发送，包括失败或完成情况未知的请求。')
        planned = self.rows[row_id]
        if planned['operation'] != operation:
            raise AuditError('操作与冻结计划不符。')
        expected = planned['payload'] if operation == 'analyze' else self.expected_payload
        if expected is None or expected != payload:
            raise AuditError('实际请求与预览不同。')
        if secret and secret in json.dumps(payload, ensure_ascii=False):
            raise AuditError('输入包含凭证，未记录或发送。')
        context = json.loads(payload['messages'][1]['content'][0]['text'])
        summary = {'question_characters': len(context.get('confirmed_question', '')),
                   'student_work_characters': len(context.get('student_work', '')),
                   'has_previous_analysis': 'previous_analysis' in context,
                   'image_count': len(context.get('attached_images', []))}
        self.put(row_id, {'row_id': row_id, 'plan_id': self.manifest['plan_id'], 'input_summary': summary,
            'mode': self.manifest['execution_mode'], 'status': 'running', 'started_at': stamp(),
            'finished_at': None, 'request_hash': digest(payload), 'payload': payload,
            'result': None, 'result_hash': None, 'call': None,
            'diagnostic_content': None, 'decision': None, 'quality_score': None})

    def finish(self, row_id, call, result, raw, secret):
        row = self.row(row_id)
        if row is None or row['status'] != 'running':
            raise AuditError('找不到本次未结束的请求。')
        call = deepcopy(call)
        if (call['kind'] == 'real_api' and call['status'] != 'ok' and call.get('http_status') is None
                and call.get('error_code') not in ('request_too_large', 'missing_key')):
            # 子进程连 started 事件都未返回时，也不能断定供应商完全没收到。
            call['completion_unknown'] = True
        valid = call['status'] == 'ok'
        if call['status'] == 'running':
            row['call'] = redact({**call, 'completion_unknown': True}, secret)
            self.put(row_id, row)
            return  # 中断或未知异常保留 running；不假装已知失败或请求没发出。
        row.update(status='reply_valid' if valid else 'failed', finished_at=stamp(),
                   call=redact(call, secret), result=redact(result, secret) if valid else None)
        row['result_hash'] = digest(row['result']) if valid else None
        if not valid and raw is not None and call.get('error_code') != 'credential_echo':
            row['diagnostic_content'] = redact(raw[:8000], secret)
        self.put(row_id, row)

    def status(self):
        counts = {name: 0 for name in ('pending', 'reply_valid', 'failed', 'running', 'blocked')}
        known, unknown, saved, rejected, usage_rows, elapsed = 0, False, 0, 0, [], 0
        failures = []
        attempts = 0
        for row_id, planned in self.rows.items():
            row = self.row(row_id)
            if row is None:
                parent = self.row(planned['depends_on']) if planned['depends_on'] else None
                counts['blocked' if parent and parent['decision'] and parent['decision']['action'] == 'reject'
                       else 'pending'] += 1
                continue
            attempts += 1
            counts[row['status']] += 1
            call = row['call'] or {}
            if row['status'] == 'failed':
                failures.append({'row_id': row_id, 'error_code': call.get('error_code'),
                                 'validation_issue': call.get('validation_issue')})
            if row['mode'] == 'real_api':
                known += call.get('attempted_requests', 0)
                unknown |= row['status'] == 'running' or call.get('completion_unknown', False)
            elapsed += call.get('elapsed_ms', 0)
            if call.get('usage') is not None:
                usage_rows.append(call['usage'])
            if row['decision']:
                saved += row['decision']['action'] == 'accept'
                rejected += row['decision']['action'] == 'reject'
        mode = self.manifest['execution_mode']
        return {'mode': 'preview' if attempts == 0 and mode == 'real_api' else mode,
            'target_mode': mode, 'plan_id': self.manifest['plan_id'],
            'planned_requests': len(self.rows), 'counts': counts, 'failures': failures, 'reserved_attempts': attempts,
            'known_real_api_attempts': known, 'real_api_calls_unknown': unknown,
            'saved': saved, 'rejected': rejected, 'elapsed_ms': round(elapsed, 2),
            'usage_recorded_rows': len(usage_rows),
            'usage_source': 'handwritten_fixture' if mode == 'local_http_test' else 'provider_if_available',
            'usage_totals': {key: sum(row[key] for row in usage_rows)
                             for key in ('input_tokens', 'output_tokens', 'total_tokens')},
            'cost_cny': None, 'quality_scores': '未评分；格式通过不等于数学正确'}
