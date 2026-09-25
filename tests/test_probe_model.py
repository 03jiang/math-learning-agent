"""单轮工具的预览、实际本机 HTTP、失败留档和人工检查边界；不请求供应商。"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from legacy.core import LearningAssistant, ValidationError
from legacy.http_test_support import LocalModelServer, successful_envelope
from legacy.model_api import ModelAPIError, ModelConfig
from legacy.probe_model import CASE, ROOT, main, probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'report'

    def read(self, name='result.json'):
        return json.loads((self.output / name).read_text())

    def test_default_preview_cannot_start_http_even_with_live_environment(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-secret-sentinel',
                                     'MATH_MODEL_CONFIG': '/missing/config.json'}), \
                patch('legacy.probe_model.ApiTutor') as tutor, patch('legacy.probe_model.LocalModelServer') as server:
            report = probe(self.output)
        tutor.assert_not_called()
        server.assert_not_called()
        self.assertEqual('preview', report['status'])
        self.assertEqual(0, report['real_api_calls'])
        self.assertIsNone(report['state_unchanged'])  # 预览尚未运行，不能声称已验证
        self.assertNotIn('test-secret-sentinel', ''.join(file.read_text() for file in self.output.iterdir()))
        request = self.read('request.json')
        self.assertEqual('incorrect', request['context']['local_checks']['answer']['status'])
        self.assertEqual('hint', request['context']['effective_settings']['explanation_mode'])

    def test_manifest_freezes_exact_case_request_and_sources(self):
        probe(self.output)
        manifest = self.read('manifest.json')
        self.assertEqual(CASE, manifest['case'])
        digest = hashlib.sha256(json.dumps(self.read('request.json'), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(digest, manifest['request_sha256'])
        for name, digest in manifest['code_hashes'].items():
            self.assertEqual(digest, hashlib.sha256((ROOT / name).read_bytes()).hexdigest())
        self.assertEqual({'manifest.json', 'request.json', 'result.json', 'review.md'},
                         {file.name for file in self.output.iterdir()})

    def test_existing_directory_file_or_symlink_prevents_all_requests(self):
        self.output.mkdir()
        marker = self.output / 'keep.txt'
        marker.write_text('keep this')
        link = self.root / 'link'
        link.symlink_to(self.output, target_is_directory=True)
        for target in (self.output, marker, link):
            with self.subTest(target=target.name), patch('legacy.probe_model.ApiTutor') as tutor, \
                    patch('legacy.probe_model.LocalModelServer') as server, self.assertRaises(ValidationError):
                probe(target, mode='real_api', config=ModelConfig(mode='api', model='fixture-model'), api_key='dummy')
            tutor.assert_not_called()
            server.assert_not_called()
        self.assertEqual('keep this', marker.read_text())

    def test_live_requires_valid_config_and_key_before_artifacts(self):
        for config, key in ((None, ''), (ModelConfig(), 'dummy'),
                            (ModelConfig(mode='api', model='fixture-model'), '')):
            with self.subTest(config=config), self.assertRaises((ValidationError, ModelAPIError)), \
                    patch('legacy.model_api.subprocess.run') as run:
                probe(self.output, mode='real_api', config=config, api_key=key)
            run.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_actual_http_once_matches_preview_and_never_confirms(self):
        server = LocalModelServer()
        daily = self.root / 'daily'
        daily.mkdir()
        sentinel = daily / 'fraction-add.json'
        sentinel.write_text('do not read or write this daily archive')
        with patch('legacy.probe_model.LocalModelServer', return_value=server), \
                patch.dict(os.environ, {'MATH_ASSISTANT_DATA_DIR': str(daily), 'OPENAI_API_KEY': 'real-secret-sentinel'}), \
                patch.object(LearningAssistant, 'confirm', side_effect=AssertionError('must not confirm')):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('reply_valid', report['status'])
        self.assertEqual(1, len(server.requests))
        payload = server.requests[0]['payload']
        actual = {'instructions': payload['instructions'], **json.loads(payload['input'])}
        self.assertEqual(self.read('request.json'), actual)
        self.assertEqual('Bearer local-test-key', server.requests[0]['authorization'])
        self.assertTrue(report['state_unchanged'])
        self.assertIsNotNone(report['round']['proposal'])
        self.assertEqual('incorrect', report['round']['local_checks']['answer']['status'])
        self.assertEqual(0, report['real_api_calls'])
        self.assertTrue(report['usage_is_synthetic'])
        self.assertEqual(150, report['model_calls'][0]['usage']['total_tokens'])
        self.assertEqual('do not read or write this daily archive', sentinel.read_text())
        saved = ''.join(file.read_text() for file in self.output.iterdir())
        self.assertNotIn('real-secret-sentinel', saved)
        self.assertNotIn('local-test-key', saved)
        with self.assertRaises(ValidationError):
            probe(self.output, mode='local_http_test')
        self.assertEqual(1, len(server.requests))

    def test_valid_without_proposal_still_passes_transport_check(self):
        server = LocalModelServer()
        envelope = successful_envelope()
        reply = json.loads(envelope['output'][1]['content'][0]['text'])
        reply.update(next_action=None, proposed_state_update=None)
        envelope['output'][1]['content'][0]['text'] = json.dumps(reply)
        server.body = envelope
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('reply_valid', report['status'])
        self.assertIsNone(report['round']['proposal'])
        self.assertTrue(report['state_unchanged'])

    def test_http_failure_records_once_without_error_body_or_update(self):
        server = LocalModelServer()
        server.status = 429
        server.body = b'secret-provider-body'
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('failed', report['status'])
        self.assertEqual('http_429', report['error_code'])
        self.assertEqual(1, len(server.requests))
        self.assertTrue(report['state_unchanged'])
        self.assertIsNone(report['round']['proposal'])
        self.assertNotIn('secret-provider-body', (self.output / 'result.json').read_text())
        self.assertEqual(report, self.read())

    def test_invalid_reply_saved_as_failure_without_raw_content(self):
        server = LocalModelServer()
        server.body = b'INVALID_RAW_SENTINEL'
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('invalid_response', report['error_code'])
        self.assertIsNone(report['round']['reply'])
        self.assertNotIn('INVALID_RAW_SENTINEL', (self.output / 'result.json').read_text())

    def test_timeout_report_keeps_unknown_completion_and_no_retry(self):
        server = LocalModelServer()
        server.stall_body, server.delay = True, 3
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('failed', report['status'])
        self.assertEqual('timeout', report['error_code'])
        self.assertTrue(report['model_calls'][0]['completion_unknown'])
        self.assertEqual(1, len(server.requests))
        self.assertTrue(report['state_unchanged'])

    def test_semantic_error_is_not_automatically_scored_as_correct(self):
        server = LocalModelServer()
        envelope = successful_envelope()
        reply = json.loads(envelope['output'][1]['content'][0]['text'])
        reply['explanation'] = '分母可以直接相加，你已经掌握分数了。'
        envelope['output'][1]['content'][0]['text'] = json.dumps(reply)
        server.body = envelope
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            report = probe(self.output, mode='local_http_test')
        self.assertEqual('reply_valid', report['status'])  # 仅结构检查；此句在语义上错误
        self.assertTrue(all(value is None for value in report['human_review'].values()))
        self.assertIn('尚未评分', (self.output / 'review.md').read_text())
        self.assertEqual('incorrect', report['round']['local_checks']['answer']['status'])

    def test_interrupt_retains_in_progress_report_instead_of_success(self):
        with patch('legacy.probe_model.Workspace.run', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            probe(self.output, mode='local_http_test')
        self.assertEqual('running', self.read()['status'])
        self.assertIsNone(self.read()['state_unchanged'])
        with self.assertRaises(ValidationError):
            probe(self.output, mode='local_http_test')

    def test_cli_preview_and_noninteractive_live_missing_key(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'mode': 'api', 'provider': 'openai', 'model': 'fixture-model',
                                      'timeout_seconds': 2, 'max_output_tokens': 2000}))
        env = {**os.environ, 'OPENAI_API_KEY': '', 'MATH_MODEL_CONFIG': str(config)}
        command = [sys.executable, str(ROOT / 'legacy/probe_model.py'), '--output', str(self.output)]
        preview = subprocess.run(command, capture_output=True, text=True, env=env, timeout=3)
        self.assertEqual(0, preview.returncode)
        self.assertEqual('preview', json.loads(preview.stdout)['mode'])
        live_output = self.root / 'live'
        live = subprocess.run([*command[:-1], str(live_output), '--live', '--config', str(config)],
                              input='', capture_output=True, text=True, env=env, timeout=3)
        self.assertEqual(2, live.returncode)
        self.assertFalse(live_output.exists())
        self.assertNotIn('Traceback', live.stderr)

    def test_cli_failure_exit_code_and_config_option_validation(self):
        server = LocalModelServer()
        server.status = 500
        with patch('legacy.probe_model.LocalModelServer', return_value=server):
            self.assertEqual(1, main(['--local-http', '--output', str(self.output)]))
        self.assertEqual('failed', self.read()['status'])
        with patch('legacy.probe_model.load_model_config') as load:
            self.assertEqual(2, main(['--config', 'ignored.json', '--output', str(self.root / 'new')]))
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
