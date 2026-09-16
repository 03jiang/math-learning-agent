"""严格返回格式的边界与本机 HTTP 回归；不代表供应商兼容性或教学质量。"""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from http_test_support import LocalModelServer
from http_worker import endpoint_kind
from model_api import ModelAPIError, chat_response_text
from study.output_contract import (OutputParseError, parse_output, endpoint, output_schema,
    strict_response_text, STRICT_ENDPOINT, FUNCTIONS)
from study.service import StudyService, PhotoTransport, build_payload, ANALYSIS_PROMPT, analysis_context
from study.run_audit import RunAudit, read_json
from study.smoke import create_run, default_config, execute, decide, notebook, verify_frozen
from tools.verify_study_live import local_fixture, run_local, main

ROOT = Path(__file__).resolve().parent


def envelope(arguments='{}', operation='analyze'):
    return {'object': 'chat.completion', 'model': 'local-fixture',
        'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'DO_NOT_RECORD_INTERNAL_REASONING',
            'tool_calls': [{'id': 'fixture-call', 'type': 'function', 'function': {
                'name': FUNCTIONS[operation], 'arguments': arguments}}]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


class OutputContractTests(unittest.TestCase):
    def test_duplicate_keys_are_rejected_even_when_equal_or_escaped(self):
        for raw in ('{"answer_feedback":"same","answer_feedback":"same"}',
                    '{"x":1,"x":2}', '{"review":{"x":1,"x":1}}',
                    '{"x":1,"\\u0078":1}'):
            with self.subTest(raw=raw), self.assertRaises(OutputParseError) as caught:
                parse_output(raw)
            self.assertEqual('duplicate_json_key', caught.exception.code)
            self.assertNotIn('answer_feedback', str(caught.exception))
        self.assertEqual({'a': {'x': 1}, 'b': {'x': 2}}, parse_output('{"a":{"x":1},"b":{"x":2}}'))

    def test_invalid_json_limits_and_constants_have_safe_error_codes(self):
        for raw, code in [('{broken', 'invalid_json'), ('{}{}', 'invalid_json'),
                          (None, 'invalid_json'), ('{"x":NaN}', 'non_standard_json'),
                          ('[Infinity,-Infinity]', 'non_standard_json'),
                          (' ' * 32001, 'reply_too_large'), ('\ud800', 'reply_too_large')]:
            with self.subTest(code=code), self.assertRaises(OutputParseError) as caught:
                parse_output(raw)
            self.assertEqual(code, caught.exception.code)

    def test_all_fixture_shapes_fit_closed_provider_schema(self):
        responses = read_json(ROOT/'evaluation/study_smoke_local_responses.json')['responses']
        for row_id, value in responses.items():
            schema = output_schema('reanalyze' if row_id in ('b06', 'b07') else 'analyze')
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(value)
        Draft202012Validator(output_schema('recognize')).validate(
            {'text': '计算 1+1', 'student_work': '', 'work_kind': 'none', 'warnings': []})
        def check(schema):
            self.assertFalse(set(schema) - {'type','properties','required','additionalProperties','enum','items'})
            if schema['type'] == 'object':
                self.assertEqual(set(schema['properties']), set(schema['required']))
                self.assertIs(False, schema['additionalProperties'])
                for child in schema['properties'].values(): check(child)
            if schema['type'] == 'array': check(schema['items'])
        for op in FUNCTIONS: check(output_schema(op))

    def test_payload_explicitly_forces_one_result_and_keeps_legacy_default(self):
        context = analysis_context('计算 1+1', '小学')
        legacy = build_payload(default_config(), ANALYSIS_PROMPT, context)
        strict = build_payload(default_config(), ANALYSIS_PROMPT, context, output_mode='strict_tool')
        self.assertIn('response_format', legacy); self.assertNotIn('tools', legacy)
        self.assertNotIn('response_format', strict)
        self.assertEqual({'type':'disabled'}, strict['thinking'])
        self.assertEqual(1, len(strict['tools']))
        self.assertIs(True, strict['tools'][0]['function']['strict'])
        self.assertEqual('return_math_analysis', strict['tool_choice']['function']['name'])
        self.assertEqual(ANALYSIS_PROMPT, legacy['messages'][0]['content'])
        self.assertEqual({**context, 'attached_images': []}, json.loads(strict['messages'][1]['content'][0]['text']))
        self.assertNotIn('attached_images', context)

    def test_incompatible_thinking_and_endpoints_fail_before_send(self):
        with self.assertRaises(ValueError): endpoint(replace(default_config(), thinking='enabled'), 'strict_tool')
        with self.assertRaises(ValueError): endpoint(default_config(), 'fallback')
        self.assertEqual('real_api', endpoint_kind(STRICT_ENDPOINT))
        for url in (STRICT_ENDPOINT+'?other=1', STRICT_ENDPOINT+'/',
                    STRICT_ENDPOINT.replace('deepseek.com','deepseek.com.example.org'),
                    'http://api.deepseek.com/beta/chat/completions'):
            with self.subTest(url=url), self.assertRaises(ValueError): endpoint_kind(url)
        with self.assertRaises(ValueError):
            StudyService(default_config(), 'unused-fixture-key', PhotoTransport(), output_mode='strict_tool')

    def test_response_is_arguments_only_and_old_parser_still_rejects_tools(self):
        record = {}
        self.assertEqual('{"ok":true}', strict_response_text(envelope('{"ok":true}'), record, 'analyze'))
        self.assertEqual(30, record['usage']['total_tokens'])
        self.assertEqual('return_math_analysis', record['output_function'])
        self.assertNotIn('reasoning', json.dumps(record))
        with self.assertRaises(ModelAPIError): chat_response_text(envelope(), {})

    def test_invalid_tool_envelopes_never_produce_candidate(self):
        variants = []
        for change in (lambda v: v['choices'][0]['message']['tool_calls'].append(
                          deepcopy(v['choices'][0]['message']['tool_calls'][0])),
                       lambda v: v['choices'][0]['message']['tool_calls'][0]['function'].update(name='save_notebook'),
                       lambda v: v['choices'][0]['message']['tool_calls'][0].update(id=''),
                       lambda v: v['choices'][0]['message'].update(content='extra answer'),
                       lambda v: v['choices'][0]['message'].update(function_call={'name':'legacy'}),
                       lambda v: v['choices'][0].update(finish_reason='stop'),
                       lambda v: v.update(choices=[])):
            value = envelope(); change(value); variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ModelAPIError): strict_response_text(value, {}, 'analyze')
        for finish, code in (('length','incomplete'), ('content_filter','refusal')):
            value = envelope(); value['choices'][0]['finish_reason'] = finish
            with self.assertRaises(ModelAPIError) as caught: strict_response_text(value, {}, 'analyze')
            self.assertEqual(code, caught.exception.code)

    def test_invalid_usage_remains_unknown(self):
        value = envelope(); value['usage']['total_tokens'] = 999
        record = {'usage': None}
        strict_response_text(value, record, 'analyze')
        self.assertIsNone(record['usage'])


class StrictHTTPTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def setup_run(self, mode='strict_tool'):
        audit = create_run(self.root/'run', mode='local_http_test', row_ids=['b01'], output_mode=mode)
        server = self.enterContext(LocalModelServer()); server.body = local_fixture(audit)
        return audit, server

    def test_complete_local_flow_requires_confirmation_and_recovers_corrections(self):
        result = run_local(self.root/'full', output_mode='strict_tool')
        self.assertEqual(7, result['counts']['reply_valid']); self.assertEqual(0, result['known_real_api_attempts'])
        checks = read_json(self.root/'full/local_checks.json')
        for key in ('notebook_absent_before_confirmation', 'rejection_kept_original_bytes',
                    'duplicate_confirmation_kept_bytes', 'reopen_preserved_correction'):
            self.assertIs(True, checks[key], key)
        audit = RunAudit(self.root/'full')
        self.assertEqual(7, checks['local_http_requests'])
        self.assertEqual('return_math_correction', audit.row('b06')['call']['output_function'])
        self.assertTrue(audit.row('b06')['call']['request_endpoint'].endswith('/beta/chat/completions'))
        self.assertNotIn('DO_NOT_RECORD_INTERNAL_REASONING', audit.path('b01').read_text())

    def test_partial_missing_reason_is_accepted_without_auto_save(self):
        audit, server = self.setup_run(); fixture = server.body
        def partial(payload):
            reply = fixture(payload); function = reply['choices'][0]['message']['tool_calls'][0]['function']
            value = json.loads(function['arguments'])
            value['student_review'].update(verdict='partial', answer_feedback='计算正确，但没有回答为何需要通分。')
            value['next_practice'] = '请说明通分理由。'
            function['arguments'] = json.dumps(value, ensure_ascii=False)
            return reply
        server.body = partial
        self.assertEqual(1, execute(audit, server=server)['counts']['reply_valid'])
        self.assertFalse(notebook(audit).directory.exists())
        self.assertEqual('/beta/chat/completions', server.requests[0]['path'])
        result = decide(audit, 'b01', 'accept', actor='local_fixture')
        self.assertTrue(result['reopen_verified'])
        self.assertEqual([], notebook(RunAudit(audit.directory)).get(result['entry_id'])['reviews'])

    def duplicate_failure(self, mode):
        audit, server = self.setup_run(mode); fixture = server.body
        def duplicate(payload):
            reply = fixture(payload); message = reply['choices'][0]['message']
            # 仿造当前真实错误的形状，内容全部为本机手写；不合并相同字段。
            if mode == 'strict_tool':
                holder = message['tool_calls'][0]['function']; field = 'arguments'
            else: holder, field = message, 'content'
            holder[field] = holder[field].replace('"answer_feedback":', '"answer_feedback":"same", "answer_feedback":', 1)
            return reply
        server.body = duplicate
        result = execute(audit, server=server)
        self.assertEqual([{'row_id':'b01', 'error_code':'invalid_content', 'validation_issue':'duplicate_json_key'}], result['failures'])
        self.assertEqual(2, audit.row('b01')['diagnostic_content'].count('"answer_feedback":'))
        self.assertIsNone(audit.row('b01')['result'])
        before = audit.path('b01').read_bytes()
        with self.assertRaises(ValueError): execute(RunAudit(audit.directory), server=server)
        with self.assertRaises(ValueError): decide(audit, 'b01', 'accept')
        self.assertEqual(before, audit.path('b01').read_bytes())
        self.assertEqual(1, len(server.requests)); self.assertFalse(notebook(audit).directory.exists())

    def test_duplicate_field_json_mode_reproduces_failure_with_precise_code(self):
        self.duplicate_failure('json_object')

    def test_duplicate_field_strict_mode_is_still_rejected_without_retry(self):
        self.duplicate_failure('strict_tool')

    def test_unexpected_schema_fields_still_fail_after_strict_response(self):
        audit, server = self.setup_run(); fixture = server.body
        def unknown(payload):
            reply = fixture(payload); fn = reply['choices'][0]['message']['tool_calls'][0]['function']
            value = json.loads(fn['arguments']); value['student_review']['diagnosis'] = []
            fn['arguments'] = json.dumps(value); return reply
        server.body = unknown
        result = execute(audit, server=server)
        self.assertEqual('student_review_fields', result['failures'][0]['validation_issue'])
        self.assertFalse(notebook(audit).directory.exists())

    def test_provider_rejection_stops_once_and_never_falls_back(self):
        audit, server = self.setup_run(); server.status = 400; server.body = {'detail':'PRIVATE_PROVIDER_ERROR'}
        result = execute(audit, server=server)
        self.assertEqual('http_400', result['failures'][0]['error_code'])
        self.assertNotIn('PRIVATE_PROVIDER_ERROR', audit.path('b01').read_text())
        with self.assertRaises(ValueError): execute(RunAudit(audit.directory), server=server)
        self.assertEqual(1, len(server.requests)); self.assertFalse(notebook(audit).directory.exists())

    def test_strict_transport_does_not_follow_redirect(self):
        audit, server = self.setup_run(); server.status = 307; server.location = server.chat_url
        result = execute(audit, server=server)
        self.assertEqual('http_307', result['failures'][0]['error_code'])
        self.assertEqual(1, len(server.requests))

    def test_frozen_mode_endpoint_and_payload_cannot_be_switched(self):
        audit, server = self.setup_run(); verify_frozen(audit)
        self.assertEqual(STRICT_ENDPOINT, audit.manifest['provider_endpoint'])
        with self.assertRaises(ValueError):
            StudyService(default_config(), 'local-test-key', PhotoTransport(server.chat_url), audit=audit)
        original = deepcopy(audit.manifest)
        for field, value in (('output_mode','json_object'), ('provider_endpoint','https://example.org'),
                             ('output_contract','other')):
            audit.manifest = deepcopy(original); audit.manifest[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): verify_frozen(audit)
        self.assertEqual([], server.requests)

    def test_cli_does_not_override_frozen_mode_or_read_key(self):
        audit, server = self.setup_run()
        with patch('getpass.getpass', side_effect=AssertionError('must not ask key')):
            self.assertEqual(2, main(['run','--directory',str(audit.directory),'--output-mode','json_object']))
        self.assertEqual([], server.requests)

    def test_image_recognition_uses_same_strict_contract_in_one_request(self):
        from study.demo_service import sample_image
        value = {'text':'计算 1+1', 'student_work':'2', 'work_kind':'answer_only', 'warnings':[]}
        with LocalModelServer() as server:
            server.body = envelope(json.dumps(value), 'recognize')
            service = StudyService(default_config(), 'local-test-key', PhotoTransport(
                server.chat_url.replace('/chat/completions','/beta/chat/completions')), output_mode='strict_tool')
            self.assertEqual(value, service.recognize(sample_image(), sample_image()))
            request = server.requests[0]['payload']
            self.assertEqual(3, len(request['messages'][1]['content']))
            self.assertEqual('return_math_transcription', request['tool_choice']['function']['name'])
            self.assertEqual(1, len(server.requests))

    def test_ui_service_uses_explicit_mode_configuration(self):
        from study import ui
        with patch.dict(os.environ, {'MATH_STUDY_DEMO':'0','MATH_PHOTO_OFFLINE':'0',
                                    'MATH_STUDY_OUTPUT_MODE':'strict_tool','DEEPSEEK_API_KEY':'fixture-key'}), \
             patch.object(ui.st, 'session_state', {}), patch.object(ui, 'StudyService') as service:
            ui.service()
            self.assertEqual('strict_tool', service.call_args.kwargs['output_mode'])


if __name__ == '__main__': unittest.main(verbosity=2)
