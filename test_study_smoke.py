"""B 阶段的运行账本、真实路径预算门禁和本机 HTTP 闭环；不请求真实模型。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from http_test_support import LocalModelServer
from model_api import ModelAPIError
from study.run_audit import RunAudit, digest, read_json, redact, write_json
from study.smoke import (create_run, default_config, execute, decide, notebook, entry_id,
                         verify_frozen)
from study.service import StudyService, PhotoTransport
from tools.verify_study_live import run_local, local_fixture, main


class StudySmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.number = 0

    def create(self, *, real=False, timeout=60):
        self.number += 1
        return create_run(self.root / f'run-{self.number}', mode='real_api' if real else 'local_http_test',
                          config=replace(default_config(), timeout_seconds=timeout))

    def server(self, audit):
        server = self.enterContext(LocalModelServer())
        server.body = local_fixture(audit)
        return server

    def replies(self):
        audit = self.create()
        server = self.server(audit)
        result = execute(audit, server=server)
        self.assertEqual(5, result['counts']['reply_valid'])
        return audit, server

    def test_preview_is_seven_planned_requests_without_model_or_notebook(self):
        with patch('study.service.PhotoTransport.send', side_effect=AssertionError('must not send')):
            audit = self.create(real=True)
        self.assertEqual('preview', audit.status()['mode'])
        self.assertEqual(7, audit.status()['counts']['pending'])
        self.assertEqual(0, audit.status()['known_real_api_attempts'])
        self.assertFalse(notebook(audit).directory.exists())
        self.assertTrue(all(row['math_score_0_1_2'] is None for row in read_json(audit.directory/'review.json')['rows']))
        for row in audit.rows.values():
            if row['payload']:
                payload = json.dumps(row['payload'], ensure_ascii=False)
                self.assertNotIn(row['human_reference'], payload)
                self.assertNotIn('reference_answer', payload)
                self.assertNotIn('human_reference', payload)
            else:
                self.assertIn(row['depends_on'], ('b02', 'b04'))
                self.assertIn('不预填示例答案', row['payload_dependency'])

    def test_no_confirmation_no_notebook_and_correction_waits(self):
        audit, server = self.replies()
        self.assertFalse(notebook(audit).directory.exists())
        execute(RunAudit(audit.directory), server=server)
        self.assertEqual(5, len(server.requests))
        self.assertIsNone(audit.row('b06'))

    def test_complete_local_http_demo_decisions_and_restart(self):
        result = run_local(self.root/'local')
        self.assertEqual(7, result['counts']['reply_valid'])
        self.assertEqual(0, result['known_real_api_attempts'])
        checks = read_json(self.root/'local/local_checks.json')
        for field in ('notebook_absent_before_confirmation', 'rejection_kept_original_bytes',
                      'duplicate_confirmation_kept_bytes', 'reopen_preserved_correction'):
            self.assertTrue(checks[field], field)
        self.assertEqual(7, checks['local_http_requests'])
        audit = RunAudit(self.root/'local')
        entries, errors = notebook(audit).list()
        self.assertEqual(4, len(entries)); self.assertEqual([], errors)
        self.assertTrue(all(entry['reviews'] == [] for entry in entries))
        self.assertEqual(1, len(notebook(audit).get(entry_id(audit, 'b02'))['corrections']))

    def test_reject_does_not_create_notebook_and_blocks_dependent_request(self):
        audit, server = self.replies()
        decide(audit, 'b02', 'reject')
        self.assertFalse(notebook(audit).directory.exists())
        execute(audit, server=server)
        self.assertEqual(5, len(server.requests))
        self.assertEqual(1, audit.status()['counts']['blocked'])
        with self.assertRaises(ValueError): decide(audit, 'b02', 'accept')

    def test_confirm_is_idempotent_and_reopen_recovers_original(self):
        audit, _ = self.replies()
        saved = decide(audit, 'b02', 'accept')
        path = notebook(audit).path(saved['entry_id']); before = path.read_bytes()
        self.assertEqual(saved, decide(RunAudit(audit.directory), 'b02', 'accept'))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(audit.row('b02')['result'], notebook(RunAudit(audit.directory)).get(saved['entry_id'])['analysis'])

    def test_saved_then_decision_log_failure_can_recover_without_duplicate(self):
        audit, _ = self.replies()
        with patch.object(audit, 'put', side_effect=OSError('disk failure after notebook commit')):
            with self.assertRaises(OSError): decide(audit, 'b02', 'accept')
        path = notebook(audit).path(entry_id(audit, 'b02')); before = path.read_bytes()
        self.assertIsNone(audit.row('b02')['decision'])
        decide(RunAudit(audit.directory), 'b02', 'accept')
        self.assertEqual(before, path.read_bytes())

    def test_changed_or_expired_candidate_cannot_save(self):
        audit, _ = self.replies()
        row = audit.row('b02'); row['result']['answer'] = 'forged'
        with audit.locked(): audit.put('b02', row)
        with self.assertRaises(ValueError): decide(audit, 'b02', 'accept')
        row = audit.row('b01')
        row['finished_at'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with audit.locked(): audit.put('b01', row)
        with self.assertRaises(ValueError): decide(audit, 'b01', 'accept')
        self.assertFalse(notebook(audit).directory.exists())

    def test_correction_uses_saved_output_not_human_reference(self):
        audit, server = self.replies()
        decide(audit, 'b02', 'accept')
        execute(audit, server=server)
        self.assertEqual(6, len(server.requests))
        request = audit.row('b06')['payload']
        context = json.loads(request['messages'][1]['content'][0]['text'])
        self.assertEqual(audit.row('b02')['result'], context['previous_analysis'])
        self.assertNotIn('human_reference', context)
        self.assertEqual(digest(request), audit.row('b06')['call']['request_hash'])

    def test_stale_saved_version_prevents_correction_request_and_save(self):
        audit, server = self.replies()
        decide(audit, 'b02', 'accept')
        book = notebook(audit); item_id = entry_id(audit, 'b02')
        book.update(item_id, 1, uuid4().hex, edit={'topic':'分数', 'reason':'尚不确定', 'correction':'用户补充'})
        before = book.path(item_id).read_bytes()
        with self.assertRaises(ValueError): execute(audit, server=server)
        self.assertEqual(5, len(server.requests)); self.assertEqual(before, book.path(item_id).read_bytes())

    def test_stale_update_after_correction_generation_cannot_overwrite(self):
        audit, server = self.replies()
        decide(audit, 'b02', 'accept'); execute(audit, server=server)
        book = notebook(audit); item_id = entry_id(audit, 'b02')
        book.update(item_id, 1, uuid4().hex, edit={'topic':'分数', 'reason':'尚不确定', 'correction':'其他修改'})
        before = book.path(item_id).read_bytes()
        with self.assertRaises(ValueError): decide(audit, 'b06', 'accept')
        self.assertEqual(before, book.path(item_id).read_bytes())

    def test_invalid_json_stops_after_one_request_and_restart_does_not_retry(self):
        audit = self.create(); server = self.server(audit)
        valid = server.body
        server.body = lambda payload: {**valid(payload), 'choices':[{'finish_reason':'stop',
            'message':{'role':'assistant','content':'{broken'}}]}
        result = execute(audit, server=server)
        self.assertEqual(1, result['counts']['failed']); self.assertEqual(6, result['counts']['pending'])
        self.assertEqual('{broken', audit.row('b01')['diagnostic_content'])
        with self.assertRaises(ValueError): execute(RunAudit(audit.directory), server=server)
        self.assertEqual(1, len(server.requests)); self.assertFalse(notebook(audit).directory.exists())

    def test_http_error_body_not_recorded_and_no_retry(self):
        audit = self.create(); server = self.server(audit)
        server.status = 401; server.body = {'private': 'local-test-key', 'detail': 'PRIVATE_ERROR_BODY'}
        execute(audit, server=server)
        content = audit.path('b01').read_text()
        self.assertNotIn('local-test-key', content); self.assertNotIn('PRIVATE_ERROR_BODY', content)
        self.assertEqual('http_401', audit.row('b01')['call']['error_code'])
        self.assertEqual(1, len(server.requests))

    def test_nested_diagnosis_stops_third_response_without_save_or_retry(self):
        # 重现真实失败的字段形状；内容来自既有手写 fixture，不公开原始响应。
        audit = self.create(); server = self.server(audit); valid = server.body
        def invalid_third(payload):
            reply = valid(payload)
            context = json.loads(payload['messages'][1]['content'][0]['text'])
            if context['student_work_kind'] == 'answer_only':
                result = json.loads(reply['choices'][0]['message']['content'])
                result['student_review']['diagnosis'] = []
                reply['choices'][0]['message']['content'] = json.dumps(result, ensure_ascii=False)
            return reply
        server.body = invalid_third
        result = execute(audit, server=server)
        self.assertEqual({'pending':4,'reply_valid':2,'failed':1,'running':0,'blocked':0},result['counts'])
        self.assertEqual(3,len(server.requests))
        self.assertEqual(200,audit.row('b03')['call']['http_status'])
        self.assertEqual('invalid_content',audit.row('b03')['call']['error_code'])
        raw = json.loads(audit.row('b03')['diagnostic_content'])
        self.assertIn('diagnosis',raw['student_review'])
        self.assertIn('diagnosis',raw)
        self.assertIsNone(audit.row('b03')['result'])
        before = audit.path('b03').read_bytes()
        with self.assertRaises(ValueError): decide(audit,'b03','accept')
        with self.assertRaises(ValueError): execute(RunAudit(audit.directory),server=server)
        self.assertEqual(before,audit.path('b03').read_bytes())
        self.assertEqual(3,len(server.requests))
        self.assertFalse(notebook(audit).directory.exists())

    def test_credential_echo_and_reasoning_never_persist(self):
        audit = self.create(); server = self.server(audit)
        fixture = server.body
        def echo(payload):
            value = fixture(payload); value['choices'][0]['message']['content'] = 'local-test-key'
            return value
        server.body = echo
        execute(audit, server=server)
        self.assertEqual('credential_echo', audit.row('b01')['call']['error_code'])
        all_text = ''.join(p.read_text() for p in audit.directory.rglob('*.json'))
        self.assertNotIn('local-test-key', all_text)
        self.assertNotIn('DO_NOT_RECORD_INTERNAL_REASONING', all_text)
        self.assertIsNone(audit.row('b01')['diagnostic_content'])

    def test_valid_response_records_model_parameters_usage_without_reasoning(self):
        audit, _ = self.replies()
        row = audit.row('b01')
        self.assertEqual('local-handwritten-fixture', row['call']['response_model'])
        self.assertEqual(audit.manifest['config']['model'], row['call']['requested_model'])
        self.assertEqual(150, row['call']['usage']['total_tokens'])
        self.assertEqual(64, len(row['call']['prompt_sha256']))
        self.assertNotIn('DO_NOT_RECORD_INTERNAL_REASONING', audit.path('b01').read_text())
        self.assertIsNone(row['quality_score']); self.assertIsNone(audit.status()['cost_cny'])

    def test_timeout_stops_and_remains_visible(self):
        audit = self.create(timeout=1); server = self.server(audit); server.delay = 1.3
        result = execute(audit, server=server)
        self.assertEqual(1, result['counts']['failed'])
        self.assertEqual('timeout', audit.row('b01')['call']['error_code'])
        self.assertEqual(1, len(server.requests))

    def test_audit_write_failure_before_send_sends_nothing(self):
        audit = self.create(); server = self.server(audit)
        with patch.object(audit, 'begin', side_effect=OSError('no disk')):
            result = execute(audit, server=server)
        self.assertIn('execution_error', result)
        self.assertEqual([], server.requests); self.assertEqual(0, result['reserved_attempts'])

    def test_finish_write_failure_leaves_running_and_never_resends(self):
        audit = self.create(); server = self.server(audit)
        with patch.object(audit, 'finish', side_effect=OSError('no disk')):
            execute(audit, server=server)
        self.assertEqual('running', audit.row('b01')['status'])
        with self.assertRaises(ValueError): execute(RunAudit(audit.directory), server=server)
        self.assertEqual(1, len(server.requests))

    def test_incomplete_real_ledger_marks_unknown_without_sending(self):
        audit = self.create(real=True)
        with audit.locked():
            audit.begin('b01', 'analyze', audit.rows['b01']['payload'], 'unused-test-key')
        result = RunAudit(audit.directory).status()
        self.assertEqual(1, result['reserved_attempts'])
        self.assertTrue(result['real_api_calls_unknown']); self.assertEqual(0, result['known_real_api_attempts'])

    def test_real_request_gate_and_cumulative_cap_with_stubbed_transport_only(self):
        audit = self.create(real=True); fixture = local_fixture(audit)
        def stub(payload, key, timeout, record):
            record.update(attempted_requests=1, http_status=200)
            return fixture(payload)
        with patch('study.smoke.verify_frozen'), patch.object(PhotoTransport, 'send', side_effect=stub) as send:
            for cap, plan, note in ((None, audit.manifest['plan_id'], '测试'), (True, audit.manifest['plan_id'], '测试'),
                                    (1, 'wrong', '测试'), (1, audit.manifest['plan_id'], None)):
                with self.assertRaises(ValueError):
                    execute(audit, key='unused-test-key', max_requests=cap, confirm_plan=plan, budget_note=note)
            self.assertEqual(0, send.call_count)
            args = dict(key='unused-test-key', max_requests=1, confirm_plan=audit.manifest['plan_id'], budget_note='单元测试，不发 HTTP')
            execute(audit, **args); execute(RunAudit(audit.directory), **args)
            self.assertEqual(1, send.call_count)
            with self.assertRaises(ValueError): execute(audit, **{**args, 'max_requests':2})
            self.assertEqual(1, send.call_count)

    def test_lost_transport_events_do_not_claim_zero_real_requests(self):
        audit = self.create(real=True)
        with patch('study.smoke.verify_frozen'), patch.object(PhotoTransport, 'send', side_effect=ModelAPIError('transport_error')):
            result = execute(audit, key='unused-test-key', max_requests=1,
                confirm_plan=audit.manifest['plan_id'], budget_note='单元测试，不发 HTTP')
        self.assertEqual(0, result['known_real_api_attempts'])
        self.assertTrue(result['real_api_calls_unknown'])
        self.assertEqual(1, result['reserved_attempts'])

    def test_invalid_final_schema_is_a_failed_record_not_a_save_candidate(self):
        audit = self.create(); server = self.server(audit); original = server.body
        def invalid(payload):
            reply = original(payload)
            reply['choices'][0]['message']['content'] = json.dumps({'answer':'unvalidated'})
            return reply
        server.body = invalid
        execute(audit, server=server)
        self.assertEqual('failed', audit.row('b01')['status'])
        self.assertIsNone(audit.row('b01')['result'])
        with self.assertRaises(ValueError): decide(audit, 'b01', 'accept')
        self.assertFalse(notebook(audit).directory.exists())

    def test_changed_preview_payload_is_rejected_before_http(self):
        audit = self.create(); server = self.server(audit)
        context = audit.rows['b01']['context']
        service = StudyService(default_config(), 'local-test-key', PhotoTransport(server.chat_url),
                               audit=audit, request_id='b01')
        with audit.locked():
            with self.assertRaises(ValueError):
                service.analyze('换成另一道题', context['school_level'], context['student_work'],
                                work_kind=context['student_work_kind'])
        self.assertEqual([], server.requests)
        self.assertIsNone(audit.row('b01'))

    def test_code_change_rejects_resume_and_dirty_source_rejects_live(self):
        audit = self.create(real=True)
        changed = deepcopy(audit.manifest['code']); changed['commit'] = 'changed'
        with patch('study.smoke.source_snapshot', return_value=changed):
            with self.assertRaises(ValueError): verify_frozen(audit)
        audit.manifest['code']['dirty'] = True
        with patch('study.smoke.source_snapshot', return_value=audit.manifest['code']):
            with self.assertRaises(ValueError): verify_frozen(audit, live=True)

    def test_refuses_existing_outputs_tampered_plan_symlink_and_concurrent_run(self):
        audit = self.create()
        with self.assertRaises(ValueError): create_run(audit.directory)
        link = self.root/'alias'; link.symlink_to(audit.directory)
        with self.assertRaises(ValueError): RunAudit(link)
        other = RunAudit(audit.directory)
        with audit.locked():
            with self.assertRaises(ValueError):
                with other.locked(): pass
        data = read_json(audit.directory/'manifest.json'); data['config']['model']='tampered'
        write_json(audit.directory/'manifest.json', data)
        with self.assertRaises(ValueError): RunAudit(audit.directory)

    def test_redaction_handles_quotes_backslashes_and_nested_values(self):
        secret = 'test-"\\-key'
        self.assertNotIn(secret, str(redact({'nested':[secret, {'text':secret}]}, secret)))

    def test_cli_defaults_to_preview_and_missing_live_approval_does_not_ask_key(self):
        output = self.root/'cli'
        with patch('study.service.PhotoTransport.send', side_effect=AssertionError('no network')):
            self.assertEqual(0, main(['--output',str(output)]))
            with patch('getpass.getpass', side_effect=AssertionError('no key prompt')):
                self.assertEqual(2, main(['run','--directory',str(output)]))


if __name__ == '__main__': unittest.main(verbosity=2)
