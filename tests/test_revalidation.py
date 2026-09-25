"""历史错误拒绝的离线复核；只用本机手写响应，不发付费请求。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from legacy.http_test_support import LocalModelServer
from study.corrections import CorrectionValidationError
from study.notebook import Notebook
from study.revalidation import create_review, decide_review, load_review
from study.run_audit import RunAudit, digest, read_json, write_json
from study.smoke import create_run, execute, decide, notebook, entry_id
from tools.verify_study_live import local_fixture
from tools.revalidate_correction import main


class RevalidationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.parent=create_run(self.root/'parent',mode='local_http_test',row_ids=['b02'],output_mode='strict_tool')
        server=self.enterContext(LocalModelServer());server.body=local_fixture(self.parent)
        execute(self.parent,server=server);decide(self.parent,'b02','accept',actor='local_fixture')
        self.failed=create_run(self.root/'failed',mode='local_http_test',row_ids=['b06'],
            output_mode='strict_tool',reuse_from=self.parent.directory)
        valid=local_fixture(self.failed)
        def combined(payload):
            reply=valid(payload)
            function=reply['choices'][0]['message']['tool_calls'][0]['function']
            value=json.loads(function['arguments'])
            value['comparison']['changes'][0]['current_excerpt']=self.failed.rows['b06']['context']['student_work']
            function['arguments']=json.dumps(value,ensure_ascii=False)
            return reply
        server.body=combined
        # 固定模拟旧校验器的错误拒绝，不篡改当前校验规则或真实记录。
        with patch('study.service.validate_result',side_effect=CorrectionValidationError('correction_current_verdict_mismatch')):
            execute(self.failed,server=server)
        self.assertEqual('failed',self.failed.row('b06')['status'])
        self.raw=self.failed.row('b06')['diagnostic_content']
        self.original=self.snapshots()
        self.review=self.root/'review'
        self.enterContext(patch('study.service.PhotoTransport.send',side_effect=AssertionError('no HTTP in revalidation')))

    def files(self,directory):
        return {str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    def snapshots(self):
        return [self.files(self.parent.directory),self.files(self.failed.directory)]

    def create(self):
        return create_review(self.failed.directory,'b06',self.review)

    def test_preview_uses_exact_raw_response_without_call_save_or_historical_rewrite(self):
        result=self.create()
        self.assertEqual(json.loads(self.raw),result['result'])
        self.assertEqual(0,result['additional_model_calls'])
        self.assertEqual('failed',result['source']['original_status'])
        self.assertFalse((self.review/'notebook').exists())
        self.assertFalse((self.review/'decision.json').exists())
        self.assertEqual(self.original,self.snapshots())
        with self.assertRaises(ValueError): self.create()

    def test_accept_is_idempotent_recovers_in_new_process_and_keeps_original(self):
        self.create()
        decision=decide_review(self.review,'accept',actor='local_fixture')
        book=Notebook(self.review/'notebook');entry=book.get(decision['entry_id'])
        self.assertEqual(2,entry['version']);self.assertEqual(1,len(entry['corrections']))
        self.assertEqual(json.loads(self.raw),entry['corrections'][0]['result'])
        self.assertEqual([],entry['reviews'])
        once=self.files(self.review)
        self.assertEqual(decision,decide_review(self.review,'accept',actor='local_fixture'))
        self.assertEqual(once,self.files(self.review))
        output=subprocess.check_output([sys.executable,'-B','-c',
            'import json,sys;from study.notebook import Notebook;print(json.dumps(Notebook(sys.argv[1]).get(sys.argv[2])))',
            str(book.directory),entry['id']],text=True,cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(entry,json.loads(output))
        self.assertEqual(self.original,self.snapshots())
        self.assertEqual(0,main(['status','--directory',str(self.review)]))

    def test_reject_changes_no_notebook_and_cannot_be_reversed(self):
        self.create();decide_review(self.review,'reject')
        self.assertFalse((self.review/'notebook').exists())
        with self.assertRaises(ValueError): decide_review(self.review,'accept')
        self.assertEqual(self.original,self.snapshots())

    def test_actual_evidence_mismatch_and_wrong_failure_type_do_not_create_candidate(self):
        row=self.failed.row('b06');value=json.loads(row['diagnostic_content'])
        value['analysis']['student_review']['comparisons'][0]['verdict']='uncertain'
        value['analysis']['student_review']['verdict']='uncertain'
        row['diagnostic_content']=json.dumps(value)
        with self.failed.locked(): self.failed.put('b06',row)
        with self.assertRaises(ValueError): self.create()
        self.assertFalse(self.review.exists())
        row['diagnostic_content']=self.raw;row['call']['validation_issue']='duplicate_json_key'
        with self.failed.locked(): self.failed.put('b06',row)
        with self.assertRaises(ValueError): self.create()
        self.assertFalse(self.review.exists())

    def test_source_or_candidate_tampering_blocks_confirmation(self):
        self.create()
        candidate=read_json(self.review/'candidate.json')
        altered=deepcopy(candidate);altered['result']['comparison']['summary']='edited'
        write_json(self.review/'candidate.json',altered)
        with self.assertRaises(ValueError): decide_review(self.review,'accept')
        write_json(self.review/'candidate.json',candidate)
        row=self.failed.row('b06');row['diagnostic_content']+=' '
        with self.failed.locked(): self.failed.put('b06',row)
        with self.assertRaises(ValueError): decide_review(self.review,'accept')
        self.assertFalse((self.review/'notebook').exists())

    def test_changed_parent_or_validator_blocks_confirmation(self):
        self.create()
        with patch('study.revalidation.source_snapshot',return_value={'different':'validator'}):
            with self.assertRaises(ValueError): decide_review(self.review,'accept')
        notebook(self.parent).update(entry_id(self.parent,'b02'),1,uuid4().hex,
            edit={'topic':'分数','reason':'尚不确定','correction':'新的编辑'})
        with self.assertRaises(ValueError): decide_review(self.review,'accept')
        self.assertFalse((self.review/'notebook').exists())

    def test_expired_revalidation_does_not_save(self):
        value=self.create()
        value['created_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        value['review_id']=digest({k:v for k,v in value.items() if k!='review_id'})
        write_json(self.review/'candidate.json',value)
        with self.assertRaises(ValueError): decide_review(self.review,'accept')
        self.assertFalse((self.review/'notebook').exists())

    def test_save_failure_and_decision_log_failure_do_not_duplicate_or_modify_source(self):
        self.create()
        with patch('study.notebook.os.replace',side_effect=OSError('save failed')):
            with self.assertRaises(OSError): decide_review(self.review,'accept')
        self.assertFalse(list((self.review/'notebook').glob('*.json')))
        with patch('study.revalidation.write_json',side_effect=OSError('decision failed')):
            with self.assertRaises(OSError): decide_review(self.review,'accept')
        once=self.files(self.review/'notebook')
        decide_review(self.review,'accept')
        self.assertEqual(once,self.files(self.review/'notebook'))
        self.assertEqual(self.original,self.snapshots())

    def test_schema_invalid_or_truncated_diagnostic_cannot_be_recovered(self):
        row=self.failed.row('b06');value=json.loads(self.raw);value['mastered']=True
        for raw in (json.dumps(value),self.raw+' '*(8000-len(self.raw))):
            row['diagnostic_content']=raw
            with self.failed.locked(): self.failed.put('b06',row)
            with self.assertRaises(ValueError): self.create()
            self.assertFalse(self.review.exists())


if __name__=='__main__': unittest.main(verbosity=2)
