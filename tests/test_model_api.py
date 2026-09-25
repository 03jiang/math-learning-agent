"""真实本机 HTTP/进程测试 + 配置与回归测试。永不调用供应商服务。"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from legacy.core import ValidationError
from legacy.curriculum import MockTutor
from legacy.http_test_support import LocalModelServer, successful_envelope
from legacy.model_api import (ApiTutor, HttpTransport, ModelAPIError, ModelConfig,
                       configured_tutor, load_model_config)
from legacy.workflow import Workspace

ROOT = Path(__file__).resolve().parents[1]


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_mock_never_uses_an_available_key(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-private-key'}), patch('legacy.model_api.subprocess.run') as run:
            self.assertIsInstance(configured_tutor(ModelConfig()), MockTutor)
            run.assert_not_called()

    def test_example_has_no_secret_and_configuration_does_not_rewrite_file(self):
        path = self.root / 'config.json'
        raw = (ROOT / 'model_config.example.json').read_bytes()
        path.write_bytes(raw)
        self.assertEqual(ModelConfig(), load_model_config(path))
        self.assertEqual(raw, path.read_bytes())
        self.assertNotIn('api_key', json.loads(raw))

    def test_missing_explicit_config_is_an_error_not_silent_fallback(self):
        with self.assertRaises(ValidationError):
            load_model_config(self.root / 'missing.json')

    def test_invalid_config_cannot_enable_network_or_save(self):
        cases = [{'mode': 'auto'}, {'provider': 'unconfigured-service'}, {'mode': 'api', 'model': ''},
                 {'timeout_seconds': 0}, {'timeout_seconds': 61}, {'timeout_seconds': True},
                 {'max_output_tokens': 0}, {'max_output_tokens': 8193}, {'model': '../ bad\nname'},
                 {'api_key': 'PRIVATE_SENTINEL'}, {'endpoint': 'https://untrusted.example'}]
        path = self.root / 'invalid.json'
        for values in cases:
            with self.subTest(values=values):
                path.write_text(json.dumps({**asdict(ModelConfig()), **values}))
                before = path.read_bytes()
                with self.assertRaises(ValidationError), patch('legacy.model_api.subprocess.run') as run:
                    configured_tutor(load_model_config(path))
                run.assert_not_called()
                self.assertEqual(before, path.read_bytes())
        for raw in ['{', '{}', 'null', ' ' * 8193, '[' * 1500 + '0' + ']' * 1500, '{"mode":"mock","mode":"api"}']:
            path.write_text(raw)
            with self.assertRaises(ValidationError):
                load_model_config(path)

    def test_missing_and_invalid_keys_fail_before_request(self):
        config = ModelConfig(mode='api', model='fixture-model')
        for key in ['', 'a\nb', ' a', '密钥', 'x' * 4097]:
            with self.subTest(key_length=len(key)), self.assertRaises(ModelAPIError), patch('legacy.model_api.subprocess.run') as run:
                ApiTutor(config, key)
            run.assert_not_called()
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}), self.assertRaises(ModelAPIError):
            configured_tutor(config)

    def test_endpoint_allowlist_rejects_custom_hosts_paths_and_credentials(self):
        for url in ['http://api.openai.com/v1/responses', 'https://untrusted.example/v1/responses',
                    'https://api.openai.com.example/v1/responses', 'http://localhost:8504/v1/responses',
                    'http://127.0.0.1:8504/other', 'http://secret@127.0.0.1:8504/v1/responses',
                    'http://127.0.0.1:8504/v1/responses?token=value']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                HttpTransport(url)

    def test_check_command_never_launches_or_exposes_key(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(asdict(ModelConfig(mode='api', model='fixture-model'))))
        for key, expected in [('', 2), ('test-private-key', 0)]:
            env = {**os.environ, 'OPENAI_API_KEY': key}
            result = subprocess.run([sys.executable, str(ROOT / 'legacy/run_app.py'), '--check', '--config', str(path)],
                                    env=env, capture_output=True, text=True, timeout=3)
            self.assertEqual(expected, result.returncode)
            self.assertEqual(bool(key), json.loads(result.stdout)['ready'])
            self.assertNotIn('test-private-key', result.stdout + result.stderr)

    def test_noninteractive_start_without_key_does_not_prompt_or_start_server(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(asdict(ModelConfig(mode='api', model='fixture-model'))))
        result = subprocess.run([sys.executable, str(ROOT / 'legacy/run_app.py'), '--config', str(path)],
                                input='', capture_output=True, text=True, timeout=3,
                                env={**os.environ, 'OPENAI_API_KEY': ''})
        self.assertEqual(2, result.returncode)
        self.assertIn('缺少模型 API 密钥', result.stderr)
        self.assertNotIn('You can now view', result.stdout)


class HttpModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.enterContext(LocalModelServer())
        self.config = ModelConfig(mode='api', model='fixture-model', timeout_seconds=2)
        self.tutor = ApiTutor(self.config, 'local-test-key', transport=HttpTransport(self.server.url))
        self.workspace = Workspace(self.root, tutor=self.tutor)

    def run_round(self, request_id='one', **kwargs):
        return self.workspace.run(request_id, 'fraction-add', '帮助我理解', **kwargs)

    def test_actual_post_payload_usage_and_confirmation(self):
        result = self.run_round()
        self.assertIsNone(result.error)
        self.assertEqual(1, len(self.server.requests))
        request = self.server.requests[0]
        self.assertEqual('/v1/responses', request['path'])
        self.assertEqual('Bearer local-test-key', request['authorization'])
        payload = request['payload']
        self.assertEqual('fixture-model', payload['model'])
        self.assertFalse(payload['store'])
        self.assertFalse(payload['stream'])
        self.assertEqual({'format': {'type': 'json_object'}}, payload['text'])
        self.assertNotIn('tools', payload)
        self.assertNotIn('local-test-key', json.dumps(payload))
        context = json.loads(payload['input'])['context']
        self.assertEqual(6, len(context['effective_settings']))
        self.assertEqual('fraction-add', context['task_id'])
        self.assertEqual('local_http_test', result.reply.origin)
        self.assertEqual(0, result.real_api_calls)
        record = result.model_calls[0]
        self.assertEqual(1, record['attempted_requests'])
        self.assertEqual({'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}, record['usage'])
        self.assertEqual('fixture-model-v1', record['response_model'])
        self.assertIsNone(record['cost'])
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.workspace.decide('fraction-add', result.proposal, 'accept')
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual('already_applied', self.workspace.decide('fraction-add', result.proposal, 'accept')[0])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual([], self.workspace.assistant('fraction-add').snapshot().task.completed_steps)

    def test_rerun_and_confirmation_never_repeat_http_request(self):
        result = self.run_round()
        self.assertIs(result, self.run_round())
        self.workspace.decide('fraction-add', result.proposal, 'reject')
        self.assertIs(result, self.run_round())
        self.assertEqual(1, len(self.server.requests))
        self.assertEqual(1, self.workspace.generation_count)
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_http_errors_do_not_retry_log_response_body_or_lose_checks(self):
        for status in [400, 401, 403, 404, 429, 500, 503]:
            with self.subTest(status=status):
                self.server.status = status
                self.server.body = {'error': 'PRIVATE_BODY_SENTINEL local-test-key'}
                before = len(self.server.requests)
                result = self.run_round(str(status), answer_submission='3/4')
                self.assertIsNone(result.proposal)
                self.assertEqual('correct', result.local_checks['answer']['status'])
                self.assertEqual(f'http_{status}', result.model_calls[0]['error_code'])
                self.assertEqual(status, result.model_calls[0]['http_status'])
                self.assertIs(result, self.run_round(str(status), answer_submission='3/4'))
                self.assertEqual(before + 1, len(self.server.requests))
        log = (self.root / 'runs.jsonl').read_text()
        self.assertNotIn('PRIVATE_BODY_SENTINEL', log)
        self.assertNotIn('local-test-key', log)
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_redirect_does_not_forward_credentials_or_send_second_request(self):
        with LocalModelServer() as target:
            self.server.status = 307
            self.server.location = target.url
            result = self.run_round()
            self.assertEqual('http_307', result.model_calls[0]['error_code'])
            self.assertEqual(1, len(self.server.requests))
            self.assertEqual([], target.requests)

    def test_missing_or_invalid_usage_stays_unknown(self):
        for index, usage in enumerate([None, {}, {'input_tokens': 0},
                                      {'input_tokens': True, 'output_tokens': 1, 'total_tokens': 2},
                                      {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 99}]):
            self.server.body['usage'] = usage
            result = self.run_round(str(index))
            self.assertIsNone(result.error)
            self.assertIsNone(result.model_calls[0]['usage'])

    def test_refused_incomplete_and_unexpected_output_are_rejected_with_usage_preserved(self):
        cases = []
        incomplete = successful_envelope()
        incomplete['status'] = 'incomplete'
        cases.append((incomplete, 'incomplete'))
        refused = successful_envelope()
        refused['output'][1]['content'] = [{'type': 'refusal', 'refusal': 'private refusal body'}]
        cases.append((refused, 'refusal'))
        tools = successful_envelope()
        tools['output'] = [{'type': 'function_call', 'name': 'write_state', 'arguments': '{}'}]
        cases.append((tools, 'invalid_response'))
        for index, (body, expected) in enumerate(cases):
            self.server.body = body
            result = self.run_round(str(index))
            self.assertIsNone(result.proposal)
            self.assertEqual(expected, result.model_calls[0]['error_code'])
            self.assertEqual(150, result.model_calls[0]['usage']['total_tokens'])

    def test_invalid_envelopes_and_oversize_body_never_write(self):
        for index, body in enumerate([b'not JSON', b'\xff', b' ' * 262145, [], {},
                                      {'status': 'completed', 'output': []}, b'{"status":"completed","status":"failed"}']):
            self.server.body = body
            result = self.run_round(str(index))
            self.assertIsNotNone(result.error)
            self.assertIsNone(result.reply)
            self.assertIsNone(result.proposal)
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_inner_content_still_requires_valid_fields_sources_and_state(self):
        for index, change in enumerate([{'cited_source_ids': ['FAKE']}, {'completed_steps': ['mastered']}, {'schema_version': 99}]):
            envelope = successful_envelope()
            reply = json.loads(envelope['output'][1]['content'][0]['text'])
            reply.update(change)
            envelope['output'][1]['content'][0]['text'] = json.dumps(reply)
            self.server.body = envelope
            result = self.run_round(str(index), solution_steps='1/2=2/6')
            self.assertEqual('invalid_content', result.model_calls[0]['error_code'])
            self.assertEqual('incorrect', result.local_checks['steps']['status'])
            self.assertIsNone(result.proposal)

    def test_reflected_credential_is_blocked_even_in_otherwise_valid_response(self):
        for index, field in enumerate(['model', 'text']):
            envelope = successful_envelope()
            if field == 'model':
                envelope['model'] = 'local-test-key'
            else:
                envelope['output'][1]['content'][0]['text'] = 'local-test-key'
            self.server.body = envelope
            result = self.run_round(str(index))
            self.assertEqual('credential_echo', result.model_calls[0]['error_code'])
            self.assertIsNone(result.reply)
        self.assertNotIn('local-test-key', (self.root / 'runs.jsonl').read_text())

    def test_blocked_response_body_is_killed_at_total_timeout_and_not_retried(self):
        self.tutor.config = replace(self.config, timeout_seconds=1)
        self.server.delay = 3
        self.server.stall_body = True
        start = time.monotonic()
        result = self.run_round(answer_submission='3/4')
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.5)
        self.assertEqual('timeout', result.model_calls[0]['error_code'])
        self.assertEqual(1, result.model_calls[0]['attempted_requests'])
        self.assertTrue(result.model_calls[0]['completion_unknown'])
        self.assertEqual('correct', result.local_checks['answer']['status'])
        self.assertIs(result, self.run_round(answer_submission='3/4'))
        self.assertEqual(1, len(self.server.requests))
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_model_wait_is_separate_from_three_second_tool_deadline(self):
        self.tutor.config = replace(self.config, timeout_seconds=5)
        self.server.delay = 3.15
        result = self.run_round()
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.proposal)
        self.assertGreater(result.model_calls[0]['elapsed_ms'], 3000)

    def test_worker_start_failure_records_zero_attempts_and_no_key_in_arguments(self):
        with patch('legacy.model_api.subprocess.run', side_effect=OSError('private-error-sentinel')) as run:
            result = self.run_round()
        self.assertEqual(0, result.model_calls[0]['attempted_requests'])
        self.assertEqual('transport_error', result.model_calls[0]['error_code'])
        self.assertNotIn('local-test-key', ' '.join(run.call_args.args[0]))
        self.assertNotIn('private-error-sentinel', result.error)

    def test_oversize_context_fails_before_any_request(self):
        assistant = self.workspace.assistant('fraction-add')
        assistant.confirm(assistant.propose(state_patch={'requirements': ['x' * 140000]}))
        before = (self.root / 'fraction-add.json').read_bytes()
        result = self.run_round()
        self.assertEqual('request_too_large', result.model_calls[0]['error_code'])
        self.assertEqual(0, result.model_calls[0]['attempted_requests'])
        self.assertEqual([], self.server.requests)
        self.assertEqual(before, (self.root / 'fraction-add.json').read_bytes())

    def test_records_are_per_round_and_old_results_do_not_change(self):
        first = self.run_round('first')
        before = deepcopy(first.model_calls)
        second = self.run_round('second')
        self.assertEqual(1, len(first.model_calls))
        self.assertEqual(1, len(second.model_calls))
        self.assertEqual(before, first.model_calls)
        self.assertEqual(2, len(self.server.requests))

    def test_real_attempt_counter_contract_with_injected_process_output_only(self):
        # 这里替换工作进程，不发送网络；验证 real_api 计数字段的算法。
        stdout = json.dumps({'event': 'started'}) + '\n' + json.dumps(
            {'event': 'result', 'status': 200, 'body': json.dumps(successful_envelope())})
        tutor = ApiTutor(self.config, 'unit-test-private-key')
        workspace = Workspace(self.root, tutor=tutor)
        with patch('legacy.model_api.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout, '')):
            result = workspace.run('counter', 'fraction-add', '帮助我')
        self.assertEqual(1, result.real_api_calls)
        self.assertEqual('real_api', result.reply.origin)
        self.assertEqual([], self.server.requests)
        self.assertNotIn('unit-test-private-key', (self.root / 'runs.jsonl').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
