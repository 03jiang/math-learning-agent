"""评估脚手架测试。所有评分均为临时目录里的测试数据，不是模型质量评估。"""
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import ValidationError
from curriculum import TASKS
from evaluate_teaching import main, run_batch
from evaluation_cases import load_cases
from evaluation_scoring import FIELDS, METRICS, summarize_scores
from http_test_support import chat_envelope


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'report'

    def scores(self):
        with (self.output / 'scores.csv').open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    def save_scores(self, rows):
        with (self.output / 'scores.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader(); writer.writerows(rows)

    def test_fixed_case_coverage_and_held_out_groups_do_not_cross_splits(self):
        self.assertEqual(48, len(load_cases()))
        self.assertEqual(36, len(load_cases('development')))
        self.assertEqual(12, len(load_cases('reserved')))
        self.assertEqual(set(TASKS), {row['task_id'] for row in load_cases('smoke')})
        dev = {(r['task_id'], r['scenario']) for r in load_cases('development')}
        held = {(r['task_id'], r['scenario']) for r in load_cases('reserved')}
        self.assertFalse(dev & held)
        self.assertTrue(any(r['reading_selections'] for r in load_cases('development')))
        self.assertTrue(any(r['reading_selections'] for r in load_cases('reserved')))

    def test_preview_has_96_requests_zero_http_even_with_key_and_empty_scores(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'SHOULD_NOT_READ'}), patch('model_api.HttpTransport.send') as send:
            summary = run_batch(self.output)
            send.assert_not_called()
        self.assertEqual(96, summary['counts']['preview'])
        self.assertEqual(0, summary['known_real_api_attempts'])
        manifest = json.loads((self.output / 'manifest.json').read_text())
        for first, second in zip(manifest['plan'][::2], manifest['plan'][1::2]):
            self.assertEqual(first['request']['context'], second['request']['context'])
            self.assertEqual({first['variant'], second['variant']}, {'baseline', 'protocol'})
            self.assertEqual({k: v for k, v in first['payload'].items() if k != 'messages'},
                             {k: v for k, v in second['payload'].items() if k != 'messages'})
        result = summarize_scores(self.output)
        self.assertEqual(96, result['unscored_rows'])
        self.assertIsNone(result['comparison']['math_correctness']['protocol_mean'])
        self.assertNotIn('SHOULD_NOT_READ', (self.output / 'manifest.json').read_text())

    def test_all_96_local_posts_pass_json_boundary_and_never_save_state(self):
        summary = run_batch(self.output, mode='local_http_test')
        self.assertEqual(96, summary['counts']['reply_valid'])
        self.assertEqual(0, summary['known_real_api_attempts'])
        reports = [json.loads(path.read_text()) for path in (self.output / 'responses').glob('*.json')]
        self.assertTrue(all(r['state_unchanged'] for r in reports))
        self.assertEqual(96, sum(r['model_calls'][0]['attempted_requests'] for r in reports))
        self.assertTrue(all(r['model_calls'][0]['kind'] == 'local_http_test' for r in reports))

    def test_revision_suite_freezes_explicit_buttons_and_partial_reading(self):
        with patch('model_api.HttpTransport.send') as send:
            preview = run_batch(self.output, suite='revision')
            send.assert_not_called()
        self.assertEqual(8, preview['counts']['preview'])
        manifest = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual({'protocol'}, {row['variant'] for row in manifest['plan']})
        self.assertEqual({'revision'}, {row['case']['split'] for row in manifest['plan']})
        self.assertEqual(['next'] * 4 + ['hint'] * 4,
                         [row['case']['help_action'] for row in manifest['plan']])
        for row in manifest['plan']:
            self.assertEqual(row['case']['help_action'], row['request']['context']['requested_help'])
            reading = row['request']['context']['reading_check']
            if reading:
                self.assertEqual(['second_reference'], reading['incorrect_question_ids'])
                self.assertEqual(['target'], reading['unanswered_question_ids'])

    def test_revision_local_http_covers_next_proposals_without_confirming(self):
        result = run_batch(self.output, mode='local_http_test', suite='revision')
        self.assertEqual(8, result['counts']['reply_valid'])
        self.assertEqual(0, result['known_real_api_attempts'])
        for name in ('r001', 'r002', 'r003', 'r004'):
            row = json.loads((self.output / 'responses' / f'{name}.json').read_text())
            self.assertTrue(row['state_unchanged'])
            self.assertIsNotNone(row['round']['proposal'])

    def test_revision_stops_on_first_missing_proposal_with_structural_diagnostics(self):
        def missing_patch(payload):
            envelope = chat_envelope(payload)
            value = json.loads(envelope['choices'][0]['message']['content'])
            value['proposed_state_update'] = None
            envelope['choices'][0]['message']['content'] = json.dumps(value)
            return envelope
        result = run_batch(self.output, mode='local_http_test', suite='revision', fixture=missing_patch)
        self.assertEqual(1, result['counts']['failed'])
        self.assertEqual(7, result['counts']['pending'])
        self.assertEqual(0, result['counts']['reply_valid'])
        reports = list((self.output / 'responses').glob('*.json'))
        self.assertEqual(1, len(reports))
        row = json.loads(reports[0].read_text())
        self.assertTrue(row['state_unchanged'])
        self.assertIsNone(row['round']['proposal'])
        self.assertEqual('missing_next_proposal', row['error_code'])
        self.assertEqual({'reason': 'missing_state_update'}, row['model_calls'][0]['validation_details'])
        self.assertEqual(1, row['model_calls'][0]['attempted_requests'])

    def test_resume_sends_only_missing_rows_and_preserves_manual_scores(self):
        first = run_batch(self.output, mode='local_http_test', suite='smoke', max_requests=1)
        self.assertEqual(3, first['counts']['pending'])
        rows = self.scores(); rows[0]['notes'] = '人工备注应保留'; self.save_scores(rows)
        response = (self.output / 'responses/r001.json').read_bytes()
        score = (self.output / 'scores.csv').read_bytes()
        resumed = run_batch(self.output, mode='local_http_test', suite='smoke', resume=True)
        self.assertEqual(4, resumed['counts']['reply_valid'])
        self.assertEqual(response, (self.output / 'responses/r001.json').read_bytes())
        self.assertEqual(score, (self.output / 'scores.csv').read_bytes())
        with patch('model_api.HttpTransport.send', side_effect=AssertionError('repeat call')):
            run_batch(self.output, mode='local_http_test', suite='smoke', resume=True)

    def test_failed_and_unknown_rows_are_not_retried_and_batch_stops(self):
        first = run_batch(self.output, mode='local_http_test', suite='smoke', fixture=lambda p: {})
        self.assertEqual(1, first['counts']['failed'])
        self.assertEqual(3, first['counts']['pending'])
        running = self.output / 'responses/r002.json'
        running.write_text(json.dumps({'status': 'running', 'real_api_calls': None}))
        saved = (self.output / 'responses/r001.json').read_bytes()
        second = run_batch(self.output, mode='local_http_test', suite='smoke', resume=True)
        self.assertEqual(2, second['counts']['reply_valid'])
        self.assertEqual(1, second['counts']['running'])
        self.assertEqual(saved, (self.output / 'responses/r001.json').read_bytes())

    def test_existing_output_and_changed_freeze_are_rejected_without_calls(self):
        run_batch(self.output, suite='smoke')
        before = (self.output / 'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):
            run_batch(self.output, suite='smoke')
        with patch('evaluate_teaching.frozen_sources', return_value={'changed.py': 'new'}), self.assertRaises(ValidationError):
            run_batch(self.output, suite='smoke', resume=True)
        with self.assertRaises(ValidationError):
            run_batch(self.output, suite='smoke', mode='local_http_test', resume=True)
        self.assertEqual(before, (self.output / 'manifest.json').read_bytes())

    def test_concurrent_resume_is_refused_before_http(self):
        import fcntl
        run_batch(self.output, mode='local_http_test', suite='smoke', max_requests=1)
        with (self.output / '.run.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('model_api.HttpTransport.send') as send, self.assertRaises(ValidationError):
                run_batch(self.output, mode='local_http_test', suite='smoke', resume=True)
            send.assert_not_called()

    def test_live_requires_request_limit_and_key_before_output_or_http(self):
        with patch('model_api.HttpTransport.send') as send:
            for kwargs in ({}, {'max_requests': 4}, {'max_requests': 500, 'api_key': 'test-key'}):
                with self.assertRaises(ValueError):
                    run_batch(self.output, mode='real_api', suite='smoke', **kwargs)
            self.assertEqual(2, main(['--live', '--output', str(self.output)]))
            send.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_scoring_uses_only_complete_pairs_and_leaves_missing_unknown(self):
        run_batch(self.output, mode='local_http_test', max_requests=2)
        rows = self.scores()
        rows[0].update({m: ('1' if rows[0][m] != 'NA' else 'NA') for m in METRICS})
        rows[0]['reviewer'] = 'TEST ONLY'
        self.save_scores(rows)
        self.assertEqual(0, summarize_scores(self.output)['comparison']['math_correctness']['paired_count'])
        rows[1].update({m: ('2' if rows[1][m] != 'NA' else 'NA') for m in METRICS})
        rows[1]['reviewer'] = 'TEST ONLY'
        self.save_scores(rows)
        result = summarize_scores(self.output)
        self.assertEqual(2, result['fully_scored_rows'])
        self.assertEqual(94, result['unscored_rows'])
        self.assertEqual(1, result['comparison']['math_correctness']['paired_count'])
        self.assertIn('不得作为简历', result['interpretation'])

    def test_scoring_rejects_unknown_duplicate_invalid_or_unattempted_scores(self):
        run_batch(self.output, suite='smoke')
        original = self.scores()
        for field, value in [('row_id', 'missing'), ('math_correctness', '3'), ('math_correctness', '2'),
                             ('math_correctness', 'NA'), ('case_id', 'changed')]:
            rows = [dict(row) for row in original]; rows[0][field] = value; self.save_scores(rows)
            with self.assertRaises(ValidationError):
                summarize_scores(self.output)
        self.save_scores([original[0]] * 4)
        with self.assertRaises(ValidationError):
            summarize_scores(self.output)


if __name__ == '__main__':
    unittest.main()
