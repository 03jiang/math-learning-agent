"""付费识图入口的离线验收；所有 HTTP 回复均手写。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from http_test_support import LocalModelServer
from study import photo_smoke
from study.run_audit import AuditError, RunAudit, read_json, write_json
from study.transcription import validate_observation
from test_study import chat_envelope

RAW={'text':'计算 1/2 + 1/4。','student_work':'2/6','work_kind':'answer_only','warnings':[]}


class PhotoSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        snapshot=photo_smoke.source_snapshot();snapshot.update(commit='frozen-test-commit',dirty=False)
        self.snapshot=snapshot
        self.enterContext(patch('study.photo_smoke.source_snapshot',return_value=deepcopy(snapshot)))

    def create(self,mode='local_http_test'):
        return photo_smoke.create_run(self.root/'run',mode=mode)

    def test_preview_sends_no_requests_or_reference_answers_and_never_overwrites(self):
        with patch('study.service.PhotoTransport.send',side_effect=AssertionError('预览不得联网')):
            audit=self.create('real_api')
        self.assertEqual(0,audit.status()['known_real_api_attempts'])
        self.assertEqual(2,audit.status()['counts']['pending'])
        for row in audit.rows.values():
            blocks=row['payload']['messages'][1]['content']
            context=json.loads(blocks[0]['text'])
            self.assertEqual({'task','provided_question','attached_images'},set(context))
            self.assertEqual('',context['provided_question'])
            self.assertEqual(2,len(blocks))
            self.assertTrue(blocks[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
        self.assertFalse((audit.directory/'notebook').exists())
        with self.assertRaises(AuditError): self.create()

    def test_local_entrypoint_uses_complete_chat_envelope_and_never_claims_real_usage(self):
        result=photo_smoke.local(self.root/'local-entrypoint')
        self.assertEqual(2,result['counts']['reply_valid'])
        self.assertEqual(0,result['counts']['failed'])
        self.assertEqual(0,result['known_real_api_attempts'])
        self.assertEqual('handwritten_fixture',result['usage_source'])

    def test_local_success_records_both_raw_outputs_and_duplicate_execution_sends_nothing(self):
        audit=self.create()
        with LocalModelServer() as server:
            server.body=chat_envelope(json.dumps(RAW))
            result=photo_smoke.execute(audit,server=server)
            self.assertEqual(2,result['counts']['reply_valid'])
            self.assertEqual(0,result['known_real_api_attempts'])
            photo_smoke.execute(RunAudit(audit.directory),server=server)
            self.assertEqual(2,len(server.requests))
        for row_id in audit.rows:
            observation=validate_observation(read_json(audit.directory/(row_id+'-observation.json')))
            self.assertEqual(RAW,observation['raw'])
            self.assertEqual('local_http_test',observation['mode'])
            self.assertIsNone(audit.row(row_id)['quality_score'])
        self.assertFalse((audit.directory/'notebook').exists())

    def test_first_failure_stops_second_request_and_restarts_cannot_retry(self):
        audit=self.create()
        with LocalModelServer() as server:
            server.body=chat_envelope('{invalid JSON')
            state=photo_smoke.execute(audit,server=server)
            self.assertEqual(1,state['counts']['failed']);self.assertEqual(1,state['counts']['pending'])
            with self.assertRaises(AuditError): photo_smoke.execute(RunAudit(audit.directory),server=server)
            self.assertEqual(1,len(server.requests))

    def test_running_row_blocks_unknown_completion(self):
        audit=self.create();row=audit.rows['b01']
        with audit.locked():
            audit.expected_payload=row['payload'];audit.begin('b01','recognize',row['payload'],'local-test-key')
        with LocalModelServer() as server:
            with self.assertRaises(AuditError): photo_smoke.execute(audit,server=server)
            self.assertEqual([],server.requests)

    def test_live_requires_explicit_plan_count_budget_and_never_uses_local_transport(self):
        audit=self.create('real_api')
        with patch('study.service.PhotoTransport.send',side_effect=AssertionError('未批准不得发送')):
            for kwargs in ({},{'confirm_plan':audit.manifest['plan_id'],'max_requests':3,'budget_note':'1元'},
                           {'confirm_plan':audit.manifest['plan_id'],'max_requests':2,'budget_note':''}):
                with self.assertRaises(AuditError): photo_smoke.execute(audit,**kwargs)
            with self.assertRaises(AuditError): photo_smoke.execute(audit,server=object())
        self.assertEqual(0,audit.status()['reserved_attempts'])
        self.assertFalse((audit.directory/'approval.json').exists())

    def test_changed_code_or_image_or_approval_prevents_live_calls(self):
        audit=self.create('real_api')
        changed=deepcopy(self.snapshot);changed['dirty']=True
        with patch('study.photo_smoke.source_snapshot',return_value=changed):
            with self.assertRaises(AuditError): photo_smoke.verify(audit,live=True)
        changed_rows=deepcopy(list(audit.rows.values()));changed_rows[0]['image']['sha256']='f'*64
        with patch('study.photo_smoke.inputs',return_value=changed_rows):
            with self.assertRaises(AuditError): photo_smoke.verify(audit,live=True)
        write_json(audit.directory/'approval.json',{'wrong':'prior approval'})
        with self.assertRaises(AuditError): photo_smoke.preflight(audit,audit.manifest['plan_id'],2,'1元')
        self.assertEqual(0,audit.status()['reserved_attempts'])

    def test_success_without_observation_requires_inspection_before_continuing(self):
        audit=self.create()
        original=photo_smoke.write_json
        def fail_observation(path,value):
            if str(path).endswith('-observation.json'): raise OSError('disk failure')
            original(path,value)
        with LocalModelServer() as server:
            server.body=chat_envelope(json.dumps(RAW))
            with patch('study.photo_smoke.write_json',side_effect=fail_observation):
                state=photo_smoke.execute(audit,server=server)
            self.assertIn('execution_error',state)
            with self.assertRaises(AuditError): photo_smoke.execute(audit,server=server)
            self.assertEqual(1,len(server.requests))


if __name__=='__main__': unittest.main(verbosity=2)
