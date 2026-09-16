"""DeepSeek 格式的实际本机 HTTP 测试；永不发送真实密钥。"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import ValidationError
from curriculum import TASKS, initial_snapshot
from http_test_support import LocalModelServer, chat_envelope
from model_api import ApiTutor, HttpTransport, configured_tutor, load_model_config, request_payload
from model_boundary import build_model_request
from workflow import Workspace

ROOT = Path(__file__).parent


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_model_config(ROOT / 'model_config.deepseek.example.json')
        self.server = self.enterContext(LocalModelServer())
        self.server.body = chat_envelope
        self.tutor = ApiTutor(self.config, 'local-test-key', transport=HttpTransport(self.server.chat_url))
        self.workspace = Workspace(self.root, tutor=self.tutor)

    def test_four_tasks_actual_chat_payload_and_confirmation(self):
        for task in TASKS:
            result = self.workspace.run(task, task, '我需要帮助', search_query='整体')
            self.assertIsNone(result.error)
            request = self.server.requests[-1]
            self.assertEqual('/chat/completions', request['path'])
            self.assertEqual('Bearer local-test-key', request['authorization'])
            payload = request['payload']
            self.assertEqual({'model', 'messages', 'response_format', 'max_tokens', 'temperature', 'stream', 'thinking'}, set(payload))
            self.assertEqual(self.config.model, payload['model'])
            self.assertEqual({'type': 'disabled'}, payload['thinking'])
            self.assertEqual({'type': 'json_object'}, payload['response_format'])
            self.assertEqual(['system', 'user'], [row['role'] for row in payload['messages']])
            self.assertIn('JSON', payload['messages'][0]['content'])
            self.assertEqual(task, json.loads(payload['messages'][1]['content'])['context']['task_id'])
            self.assertEqual(150, result.model_calls[0]['usage']['total_tokens'])
            self.assertEqual(0, result.real_api_calls)
            self.assertFalse((self.root / f'{task}.json').exists())
            self.workspace.decide(task, result.proposal, 'accept')
            before = (self.root / f'{task}.json').read_bytes()
            self.assertEqual('already_applied', self.workspace.decide(task, result.proposal, 'accept')[0])
            self.assertEqual(before, (self.root / f'{task}.json').read_bytes())
            self.assertEqual([], Workspace(self.root).assistant(task).snapshot().task.completed_steps)
        self.assertEqual(4, len(self.server.requests))
        self.assertNotIn('PRIVATE_REASONING_FIXTURE', (self.root / 'runs.jsonl').read_text())

    def test_wrong_reading_fields_enter_wire_and_do_not_change_progress(self):
        selected = {'whole': 'original', 'second_reference': 'remaining'}
        result = self.workspace.run('reading', 'fraction-word', '', reading_selections=selected, help_action='hint')
        reading = json.loads(self.server.requests[0]['payload']['messages'][1]['content'])['context']['reading_check']
        self.assertEqual(['second_reference'], reading['incorrect_question_ids'])
        self.assertEqual(['target'], reading['unanswered_question_ids'])
        self.assertEqual('remaining', reading['selections']['second_reference'])
        self.assertEqual(reading, result.local_checks['reading'])
        self.assertIs(result, self.workspace.run('reading', 'fraction-word', '', reading_selections=selected, help_action='hint'))
        with self.assertRaises(ValidationError):
            self.workspace.run('reading', 'fraction-word', '', reading_selections={}, help_action='hint')
        self.workspace.decide('fraction-word', result.proposal, 'reject')
        self.assertFalse((self.root / 'fraction-word.json').exists())
        self.assertEqual(1, len(self.server.requests))

    def test_invalid_reading_is_rejected_before_http_or_log(self):
        for selected in ({'whole': []}, {'whole': 'fake'}, {'incorrect_question_ids': []}, {'target': True}):
            with self.assertRaises(ValidationError):
                self.workspace.run('invalid', 'fraction-word', '帮助', reading_selections=selected)
        with self.assertRaises(ValidationError):
            self.workspace.run('wrong-task', 'fraction-add', '帮助', reading_selections={})
        self.assertEqual([], self.server.requests)
        self.assertFalse(self.root.joinpath('runs.jsonl').exists())

    def test_bad_chat_envelopes_do_not_write_and_keep_local_check(self):
        snapshot = initial_snapshot('fraction-word')
        payload = request_payload(self.config, build_model_request('帮助', snapshot, snapshot.settings, []))
        original = chat_envelope(payload)
        cases = []
        for finish in ('length', 'content_filter', 'tool_calls', 'aborted', 'insufficient_system_resource'):
            bad = deepcopy(original); bad['choices'][0]['finish_reason'] = finish; cases.append(bad)
        for content in ('', 'not JSON', '{"schema_version":1}', None):
            bad = deepcopy(original); bad['choices'][0]['message']['content'] = content; cases.append(bad)
        for change in ({'completed_steps': ['mastered']}, {'reading_check': {'status': 'matched'}}, {'cited_source_ids': ['FAKE']}):
            bad = deepcopy(original)
            reply = json.loads(bad['choices'][0]['message']['content']); reply.update(change)
            bad['choices'][0]['message']['content'] = json.dumps(reply); cases.append(bad)
        for i, bad in enumerate(cases + [{}, {'object': 'chat.completion', 'choices': []}]):
            self.server.body = bad
            result = self.workspace.run(str(i), 'fraction-word', '帮助', answer_submission='1/4', reading_selections={'whole': 'remaining'})
            self.assertIsNotNone(result.error)
            self.assertIsNone(result.proposal)
            self.assertEqual(['whole'], result.local_checks['reading']['incorrect_question_ids'])
            self.assertEqual('correct', result.local_checks['answer']['status'])
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_no_retry_on_http_error_and_no_secret_or_error_body_in_log(self):
        self.server.status = 429
        self.server.body = {'error': 'PRIVATE_BODY local-test-key'}
        result = self.workspace.run('error', 'fraction-add', '', help_action='next')
        self.assertEqual('http_429', result.model_calls[0]['error_code'])
        self.workspace.run('error', 'fraction-add', '', help_action='next')
        self.assertEqual(1, len(self.server.requests))
        log = (self.root / 'runs.jsonl').read_text()
        self.assertNotIn('PRIVATE_BODY', log)
        self.assertNotIn('local-test-key', log)

    def test_config_endpoint_model_and_deepseek_key_are_selected(self):
        for base in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
            config = replace(self.config, base_url=base, model='example-model-id')
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key', 'OPENAI_API_KEY': ''}):
                tutor = configured_tutor(config)
            self.assertEqual(base + '/chat/completions', tutor.transport.endpoint)
            self.assertEqual('example-model-id', tutor.config.model)
        for kwargs in ({'base_url': 'https://untrusted.example'}, {'api_format': 'responses'}, {'temperature': True}):
            with self.assertRaises(ValidationError):
                replace(self.config, **kwargs)

    def test_baseline_and_protocol_share_context_and_generation_parameters(self):
        snap = initial_snapshot('fraction-word')
        requests = [build_model_request('帮助', snap, snap.settings, [], prompt_variant=v,
                                       reading_selections={'target': 'used_fraction'}) for v in ('baseline', 'protocol')]
        self.assertEqual(requests[0]['context'], requests[1]['context'])
        self.assertNotEqual(requests[0]['instructions'], requests[1]['instructions'])
        bodies = [request_payload(self.config, r) for r in requests]
        self.assertEqual({k: v for k, v in bodies[0].items() if k != 'messages'},
                         {k: v for k, v in bodies[1].items() if k != 'messages'})


if __name__ == '__main__':
    unittest.main()
