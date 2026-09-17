"""识图来源 → 核对输入 → 分析 → 接受/拒绝 → 重启；只用本机 HTTP 手写响应。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from http_test_support import LocalModelServer
from study import photo_smoke
from study.photo_analysis import confirm_inputs, input_confirmation
from study.run_audit import RunAudit, AuditError, read_json, write_json, digest
from study.smoke import create_run, execute, decide, notebook, source_snapshot, verify_frozen
from tools.verify_study_live import local_fixture, run_local, main


class PhotoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        snapshot=source_snapshot();snapshot.update(commit='test-frozen-commit',dirty=False)
        self.enterContext(patch('study.smoke.source_snapshot',return_value=snapshot))
        self.enterContext(patch('study.photo_smoke.source_snapshot',return_value=snapshot))
        state=photo_smoke.local(self.root/'ocr')
        self.assertEqual(2,state['counts']['reply_valid'])
        self.source=RunAudit(self.root/'ocr')
        self.original=self.bytes(self.source.directory)
        self.server=self.enterContext(LocalModelServer())

    def bytes(self,directory):
        return {str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    def create(self,name='analysis',**kwargs):
        return create_run(self.root/name,mode='local_http_test',ocr_from=self.source.directory,
                          output_mode='strict_tool',**kwargs)

    def analyze(self,audit):
        confirm_inputs(audit,actor='local_fixture')
        self.server.body=local_fixture(audit)
        return execute(audit,server=self.server)

    def test_preview_has_exact_two_ocr_inputs_and_images_but_no_reference_metadata(self):
        with patch('study.service.PhotoTransport.send',side_effect=AssertionError('preview must not send')):
            audit=self.create()
        self.assertEqual(2,len(audit.rows));self.assertEqual(0,audit.status()['reserved_attempts'])
        for row_id,row in audit.rows.items():
            blocks=row['payload']['messages'][1]['content'];context=json.loads(blocks[0]['text'])
            self.assertEqual(self.source.row(row_id)['result']['text'],context['confirmed_question'])
            self.assertEqual(self.source.row(row_id)['result']['student_work'],context['student_work'])
            self.assertEqual({'confirmed_question','school_level','student_work','student_work_kind','attached_images'},set(context))
            self.assertEqual('data:image/jpeg;base64,'+row['image']['base64'],blocks[1]['image_url']['url'])
            self.assertTrue(row['payload']['tools'][0]['function']['strict'])
        self.assertFalse((audit.directory/'input-confirmation.json').exists())
        self.assertFalse(notebook(audit).directory.exists())
        self.assertEqual(self.original,self.bytes(self.source.directory))

    def test_unconfirmed_inputs_never_send_or_save_and_confirming_alone_does_not_analyze(self):
        audit=self.create();self.server.body=local_fixture(audit)
        with self.assertRaises(AuditError): execute(audit,server=self.server)
        self.assertEqual([],self.server.requests);self.assertEqual(0,audit.status()['reserved_attempts'])
        first=confirm_inputs(audit,actor='local_fixture');before=(audit.directory/'input-confirmation.json').read_bytes()
        self.assertEqual(first,confirm_inputs(RunAudit(audit.directory),actor='local_fixture'))
        self.assertEqual(before,(audit.directory/'input-confirmation.json').read_bytes())
        self.assertEqual([],self.server.requests);self.assertFalse(notebook(audit).directory.exists())

    def test_real_plan_cannot_use_mock_ocr_and_scope_cannot_expand(self):
        with self.assertRaises(AuditError): create_run(self.root/'real',ocr_from=self.source.directory)
        for kwargs in ({'row_ids':['b01']},{'reuse_from':self.source.directory}):
            with self.assertRaises(AuditError): self.create(**kwargs)
        self.assertFalse((self.root/'analysis').exists());self.assertFalse((self.root/'real').exists())

    def test_invalid_or_incomplete_source_is_rejected_before_output_creation(self):
        old=self.source.row('b01')
        for field,value in [('status','failed'),('result',{'wrong':'shape'}),('request_hash','wrong')]:
            row=deepcopy(old);row[field]=value
            with self.source.locked(): self.source.put('b01',row)
            with self.assertRaises((ValueError,KeyError)): self.create()
            self.assertFalse((self.root/'analysis').exists())
        with self.source.locked(): self.source.put('b01',old)
        path=self.source.directory/'b01-observation.json'
        observation=read_json(path);observation['raw']['student_work']='3/4';write_json(path,observation)
        with self.assertRaises(ValueError): self.create()

    def test_forged_source_image_even_with_valid_image_hash_cannot_replace_sent_photo(self):
        from study.images import transform_image
        manifest=deepcopy(self.source.manifest)
        manifest['rows'][0]['image']=transform_image(manifest['rows'][0]['image'],horizontal=(0,50))
        manifest['plan_id']=digest({k:v for k,v in manifest.items() if k!='plan_id'})
        write_json(self.source.directory/'manifest.json',manifest)
        for row_id in ('b01','b02'):
            row=read_json(self.source.path(row_id));row['plan_id']=manifest['plan_id'];write_json(self.source.path(row_id),row)
        with self.assertRaises(AuditError): self.create()

    def test_success_requires_separate_save_restores_schema4_and_keeps_raw_source(self):
        audit=self.create();state=self.analyze(audit)
        self.assertEqual(2,state['counts']['reply_valid']);self.assertEqual(2,len(self.server.requests))
        self.assertFalse(notebook(audit).directory.exists())
        for row_id,row in audit.rows.items():
            self.assertEqual(row['payload'],audit.row(row_id)['payload'])
        decision=decide(audit,'b01','accept',actor='local_fixture')
        entry=notebook(audit).get(decision['entry_id'])
        self.assertEqual(4,entry['schema_version']);self.assertEqual([],entry['reviews'])
        self.assertEqual(audit.rows['b01']['observation'],entry['transcription']['observation'])
        self.assertEqual(audit.rows['b01']['image'],entry['image'])
        self.assertEqual([],entry['transcription']['changed_fields'])
        before=self.bytes(audit.directory)
        decide(RunAudit(audit.directory),'b01','accept',actor='local_fixture')
        self.assertEqual(before,self.bytes(audit.directory))
        saved=json.loads(subprocess.check_output([sys.executable,'-B','-c',
            'from study.notebook import Notebook; import json,sys; print(json.dumps(Notebook(sys.argv[1]).get(sys.argv[2])))',
            str(notebook(audit).directory),entry['id']],text=True,cwd=Path(__file__).resolve().parent))
        self.assertEqual(entry,saved)
        self.assertEqual(self.original,self.bytes(self.source.directory))

    def test_rejection_does_not_create_notebook_or_change_source(self):
        audit=self.create();self.analyze(audit)
        for row_id in audit.rows: decide(audit,row_id,'reject',actor='local_fixture')
        self.assertFalse(notebook(audit).directory.exists())
        self.assertEqual(self.original,self.bytes(self.source.directory))
        with self.assertRaises(AuditError): decide(audit,'b01','accept')

    def test_source_change_after_preview_blocks_both_send_and_save(self):
        audit=self.create();self.analyze(audit)
        pending=self.create('pending');confirm_inputs(pending,actor='local_fixture')
        row=self.source.row('b01');row['call']['elapsed_ms']+=1
        with self.source.locked(): self.source.put('b01',row)
        with self.assertRaises(AuditError): execute(pending,server=self.server)
        with self.assertRaises(AuditError): decide(audit,'b01','accept')
        self.assertEqual(2,len(self.server.requests));self.assertFalse(notebook(audit).directory.exists())

    def test_changed_or_missing_confirmation_blocks_requests_and_saving(self):
        audit=self.create();self.analyze(audit)
        pending=self.create('pending');value=confirm_inputs(pending,actor='local_fixture')
        value['traces']['b01']['confirmed']['work_kind']='answer_only'
        value['confirmation_id']=digest({k:v for k,v in value.items() if k!='confirmation_id'})
        write_json(pending.directory/'input-confirmation.json',value)
        with self.assertRaises(ValueError): execute(pending,server=self.server)
        (audit.directory/'input-confirmation.json').unlink()
        with self.assertRaises(AuditError): decide(audit,'b01','accept')
        with self.assertRaises(AuditError): confirm_inputs(audit,actor='local_fixture')
        self.assertEqual(2,len(self.server.requests));self.assertFalse(notebook(audit).directory.exists())

    def test_first_failure_stops_without_retry_or_auto_save(self):
        audit=self.create();confirm_inputs(audit,actor='local_fixture')
        self.server.body={'object':'chat.completion','choices':[]}
        state=execute(audit,server=self.server)
        self.assertEqual(1,state['counts']['failed']);self.assertEqual(1,state['counts']['pending'])
        with self.assertRaises(AuditError): execute(RunAudit(audit.directory),server=self.server)
        self.assertEqual(1,len(self.server.requests));self.assertFalse(notebook(audit).directory.exists())

    def test_answer_only_inference_is_rejected_in_actual_photo_analysis_path(self):
        audit=self.create();confirm_inputs(audit,actor='local_fixture')
        valid_reply=local_fixture(audit)
        def bad_reply(payload):
            envelope=valid_reply(payload)
            function=envelope['choices'][0]['message']['tool_calls'][0]['function']
            value=json.loads(function['arguments'])
            if value['student_review']['work_kind']=='answer_only':
                value['student_review']['observed_approach']='你把分母相加了。'
            function['arguments']=json.dumps(value,ensure_ascii=False)
            return envelope
        self.server.body=bad_reply
        state=execute(audit,server=self.server)
        self.assertEqual(1,state['counts']['reply_valid']);self.assertEqual(1,state['counts']['failed'])
        with self.assertRaises(AuditError): decide(audit,'b02','accept')
        self.assertFalse(notebook(audit).directory.exists())

    def test_live_cli_missing_input_confirmation_stops_before_password_prompt(self):
        audit=self.create()
        # 仅模拟 CLI 的真实模式分支；不冒充真实供应商结果。
        audit.manifest['execution_mode']='real_api'
        error=io.StringIO()
        with patch('tools.verify_study_live.RunAudit',return_value=audit), \
             patch('tools.verify_study_live.verify_frozen'), \
             patch('tools.verify_study_live.getpass.getpass',side_effect=AssertionError('must not read key')), \
             redirect_stderr(error):
            result=main(['run','--directory',str(audit.directory),'--confirm-plan',audit.manifest['plan_id'],
                         '--max-requests','2','--budget-note','test only'])
        self.assertEqual(2,result)
        self.assertIn('先确认两份识图文字',error.getvalue())
        self.assertEqual([],self.server.requests)

    def test_bad_partial_step_stops_photo_run_and_preserves_original_reply(self):
        audit=self.create();confirm_inputs(audit,actor='local_fixture')
        fixture=local_fixture(audit)
        def wrong_step(payload):
            envelope=fixture(payload);holder=envelope['choices'][0]['message']['tool_calls'][0]['function']
            value=json.loads(holder['arguments'])
            value['student_review']['comparisons'][1].update(student_excerpt='= 2/6',verdict='incorrect')
            holder['arguments']=json.dumps(value,ensure_ascii=False)
            return envelope
        self.server.body=wrong_step
        state=execute(audit,server=self.server)
        self.assertEqual(1,state['counts']['failed']);self.assertEqual(1,state['counts']['pending'])
        row=audit.row('b01')
        self.assertEqual('step_quote_incomplete',row['call']['validation_issue'])
        self.assertIsNone(row['result'])
        raw=json.loads(row['diagnostic_content'])
        self.assertEqual('= 2/6',raw['student_review']['comparisons'][1]['student_excerpt'])
        with self.assertRaises(AuditError): execute(audit,server=self.server)
        with self.assertRaises(AuditError): decide(audit,'b01','accept')
        self.assertEqual(1,len(self.server.requests));self.assertFalse(notebook(audit).directory.exists())

    def test_inferred_answer_feedback_is_rejected_in_json_and_strict_photo_paths(self):
        for mode in ('json_object','strict_tool'):
            audit=create_run(self.root/mode,mode='local_http_test',ocr_from=self.source.directory,output_mode=mode)
            confirm_inputs(audit,actor='local_fixture');fixture=local_fixture(audit)
            def bad_feedback(payload):
                envelope=fixture(payload);msg=envelope['choices'][0]['message']
                holder,key=(msg['tool_calls'][0]['function'],'arguments') if mode=='strict_tool' else (msg,'content')
                value=json.loads(holder[key])
                if value['student_review']['work_kind']=='answer_only':
                    value['student_review']['answer_feedback']='2/6 是把分子、分母分别相加得到的。'
                holder[key]=json.dumps(value,ensure_ascii=False)
                return envelope
            self.server.body=bad_feedback
            state=execute(audit,server=self.server)
            self.assertEqual(1,state['counts']['reply_valid']);self.assertEqual(1,state['counts']['failed'])
            self.assertEqual('answer_only_feedback_not_bounded',audit.row('b02')['call']['validation_issue'])
            count=len(self.server.requests)
            with self.assertRaises(AuditError): execute(audit,server=self.server)
            with self.assertRaises(AuditError): decide(audit,'b02','accept')
            self.assertEqual(count,len(self.server.requests));self.assertFalse(notebook(audit).directory.exists())

    def test_expired_or_tampered_analysis_does_not_save(self):
        audit=self.create();self.analyze(audit)
        old=audit.row('b01')
        bad=deepcopy(old);bad['result']['answer']='1'
        with audit.locked(): audit.put('b01',bad)
        with self.assertRaises(AuditError): decide(audit,'b01','accept')
        old['finished_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        with audit.locked(): audit.put('b01',old)
        with self.assertRaises(AuditError): decide(audit,'b01','accept')
        self.assertFalse(notebook(audit).directory.exists())

    def test_decision_log_failure_recovers_same_trace_without_duplicate_notebook(self):
        audit=self.create();self.analyze(audit)
        with patch.object(audit,'put',side_effect=OSError('decision log failed')):
            with self.assertRaises(OSError): decide(audit,'b01','accept',actor='local_fixture')
        before=self.bytes(notebook(audit).directory)
        decide(RunAudit(audit.directory),'b01','accept',actor='local_fixture')
        self.assertEqual(before,self.bytes(notebook(audit).directory))
        self.assertEqual(1,len(notebook(audit).list()[0]))

    def test_successful_requests_are_never_resent(self):
        audit=self.create();self.analyze(audit)
        execute(RunAudit(audit.directory),server=self.server)
        self.assertEqual(2,len(self.server.requests))
        self.assertEqual(0,audit.status()['known_real_api_attempts'])

    def test_local_entrypoint_covers_confirmation_reject_accept_and_reopen(self):
        result=run_local(self.root/'local-entry',output_mode='strict_tool',ocr_from=self.source.directory)
        self.assertEqual(2,result['counts']['reply_valid']);self.assertEqual(1,result['saved']);self.assertEqual(1,result['rejected'])
        checks=read_json(self.root/'local-entry/local_checks.json')
        for key in ('notebook_absent_before_confirmation','rejection_kept_notebook_absent','duplicate_confirmation_kept_bytes',
                    'schema_4_restored','raw_ocr_restored','self_reviews_empty'):
            self.assertTrue(checks[key],key)
        self.assertEqual(self.original,self.bytes(self.source.directory))

    def test_cli_cannot_replace_ocr_source_at_execution_or_read_key_for_mock_run(self):
        audit=self.create()
        with patch('tools.verify_study_live.getpass.getpass',side_effect=AssertionError('no key')):
            self.assertEqual(2,main(['run','--directory',str(audit.directory),'--ocr-from',str(self.source.directory)]))
            self.assertEqual(2,main(['run','--directory',str(audit.directory),'--confirm-plan',audit.manifest['plan_id'],
                                     '--max-requests','2','--budget-note','test only']))


if __name__=='__main__': unittest.main(verbosity=2)
