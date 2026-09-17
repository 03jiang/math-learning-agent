"""独立的私有执行日志；工具本身不写文件，日志不等于错题收藏。"""
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import os
import re

from study.run_audit import read_json, write_json, redact, stamp, digest
from study.agent_protocol import AgentError


class AgentAudit:
    def __init__(self, directory):
        self.directory=Path(directory)
        if self.directory.is_symlink(): raise AgentError('unsafe_audit_directory')

    def path(self, run_id):
        if type(run_id) is not str or not re.fullmatch(r'[a-f0-9]{32}',run_id):
            raise AgentError('invalid_run_id')
        return self.directory/(run_id+'.json')

    def read(self, run_id):
        path=self.path(run_id)
        if path.is_symlink() or path.stat().st_size>2_000_000: raise AgentError('invalid_run_log')
        value=read_json(path)
        if value.get('run_id')!=run_id: raise AgentError('invalid_run_log')
        return value

    def begin(self, value, key):
        path=self.path(value['run_id'])
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        # 不续跑已有 round ID；进程中断留下 running 也不能重发。
        try: fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError: raise AgentError('run_already_reserved') from None
        with os.fdopen(fd,'w') as stream:
            json.dump(redact(value,key),stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())

    def save(self, value, key):
        write_json(self.path(value['run_id']),redact(value,key))

    @contextmanager
    def locked(self, run_id):
        lock=self.path(run_id).with_suffix('.lock')
        if lock.is_symlink(): raise AgentError('unsafe_audit_lock')
        with lock.open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield


def assert_candidate(audit, run_id, result, scope):
    value=audit.read(run_id)
    if value['status'] not in ('success','needs_clarification') or value['final'] is None:
        raise AgentError('no_valid_agent_candidate')
    if value['final_hash']!=digest(value['final']) or value['final']['result']!=result:
        raise AgentError('agent_candidate_changed')
    if value['decision']:
        if value['decision']['action']=='reject': raise AgentError('agent_candidate_rejected')
        return value
    if datetime.now(timezone.utc)-datetime.fromisoformat(value['finished_at'])>timedelta(hours=24):
        raise AgentError('agent_candidate_expired')
    if value['scope']!=scope.signature(): raise AgentError('agent_sources_changed')
    return value


def record_decision(audit, run_id, action, *, saved_entry=None, operation_id=None):
    if action not in ('accept','reject'): raise AgentError('invalid_agent_decision')
    with audit.locked(run_id):
        value=audit.read(run_id)
        if value['status'] not in ('success','needs_clarification') or value['final_hash']!=digest(value['final']):
            raise AgentError('no_valid_agent_candidate')
        entry_id=saved_entry['id'] if saved_entry else None
        if action=='accept':
            if saved_entry is None: raise AgentError('agent_save_not_verified')
            context=value['context']
            if (saved_entry['question']!=context['confirmed_question'] or saved_entry['level']!=context['school_level']):
                raise AgentError('agent_save_not_verified')
            if value['operation']=='analyze':
                stored=saved_entry['analysis']; work=saved_entry['my_work']
            else:
                row=next((r for r in saved_entry.get('corrections',[]) if r['id']==operation_id),None)
                if row is None: raise AgentError('agent_save_not_verified')
                stored=row['result'];work=row['answer']
            if stored!=value['final']['result'] or work!=context['student_work']:
                raise AgentError('agent_save_not_verified')
        elif saved_entry is not None: raise AgentError('invalid_agent_decision')
        decision={'action':action,'entry_id':entry_id,'operation_id':operation_id}
        if value['decision']:
            if any(value['decision'].get(k)!=v for k,v in decision.items()): raise AgentError('agent_decision_conflict')
            return value['decision']
        value['decision']={**decision,'at':stamp()}
        audit.save(value,'')
        return value['decision']
