"""Offline-only tests. Synthetic scoring examples are never portfolio results."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from http_test_support import LocalModelServer
from study.context import remember, select_next
from study.evaluation import (DIMENSIONS, RecordingLocalTransport, freeze, handwritten_response,
                              report, run_local, seed_scope, verify)
from study.evaluation_baseline import FixedWorkflowService, rule_queries
from study.evaluation_cases import inventory, load_cases, model_context, selected_cases, task_for_turn
from study.evaluation_scoring import summarize, score_report
from study.run_audit import AuditError, digest, read_json, stamp, write_json
from study.service import ANALYSIS_PROMPT, PhotoTransport
from study.smoke import default_config
from tools.evaluate_agent import main


class CasesTests(unittest.TestCase):
    def test_counts_do_not_claim_independent_students_or_48_new_questions(self):
        cases = load_cases()
        self.assertEqual(20, len(selected_cases()))
        self.assertEqual(10, len(selected_cases('reserved')))
        info = inventory(cases)
        self.assertEqual(30, info['cases'])
        self.assertEqual(15, info['families'])
        self.assertEqual(15, info['distinct_confirmed_questions'])
        self.assertEqual(0, info['student_participants'])
        self.assertEqual(8, info['multi_turn_cases'])
        self.assertEqual(47, info['conversation_turns_per_arm'])
        self.assertEqual(235, info['max_model_requests_total'])

    def test_cross_split_family_and_question_are_rejected_including_switch(self):
        from study.evaluation_cases import ASSETS
        for mutate in (
            lambda d: d['cases'][20]['turns'][0].update(family_id='linear_equation'),
            lambda d: d['cases'][20]['turns'][0].update(question=d['cases'][0]['turns'][0]['question']),
            lambda d: d['cases'][7]['turns'][-1].update(family_id='probability')):
            data = read_json(ASSETS / 'cases.json')
            mutate(data)
            with patch('study.evaluation_cases.read_json', return_value=data), self.assertRaises(AuditError):
                load_cases()

    def test_rubrics_transcription_and_labels_never_enter_either_request(self):
        for case in load_cases():
            poisoned = deepcopy(case)
            poisoned['human_only'] = {'reference': 'GOLD_MUST_NOT_LEAK'}
            poisoned['transcription'] = {'raw': 'OCR_MUST_NOT_LEAK'}
            poisoned['tags'] = ['LABEL_MUST_NOT_LEAK']
            for turn in case['turns']:
                contexts = []
                for arm in ('A', 'B'):
                    context = model_context(poisoned, turn, task_for_turn(None, case, turn), arm)
                    raw = json.dumps(context)
                    self.assertNotIn('MUST_NOT_LEAK', raw)
                    self.assertNotIn('human_only', raw)
                    self.assertEqual(turn['student_work'], context['student_work'])
                    contexts.append(context)
                self.assertEqual(contexts[0], contexts[1])

    def test_recent_context_difference_is_explicit_and_switch_clears_task(self):
        case = load_cases()[7]
        turn = case['turns'][0]
        task = task_for_turn(None, case, turn)
        for n in range(4):
            remember(task, str(n), 'student', 'reply', 'fixture')
        reply = {'schema_version': 1, 'status': 'explained', 'reply': '说明', 'next_step': '下一步', 'clarification': ''}
        select_next(task, reply)
        self.assertEqual(1, len(model_context(case, turn, task, 'A')['learning']['recent_dialogue']))
        self.assertEqual(4, len(model_context(case, turn, task, 'B')['learning']['recent_dialogue']))
        self.assertEqual([], task['completed_steps'])
        changed = task_for_turn(task, case, case['turns'][-1])
        self.assertEqual([], changed['turns'])
        self.assertEqual('', changed['selected_next_step'])

    def test_required_coverage_and_sane_rule_baseline(self):
        tags = {t for c in load_cases() for t in c['tags']}
        self.assertTrue({'correct', 'incorrect', 'answer_only', 'missing_condition', 'multi_turn',
                         'history_old', 'history_disabled', 'retrieval_no_results', 'question_switch',
                         'transcription_corrected', 'transcription_unresolved', 'unauthorized_request',
                         'injected_tool_failure'} <= tags)
        case = load_cases()[0]
        context = model_context(case, case['turns'][0], task_for_turn(None, case, case['turns'][0]), 'A')
        self.assertEqual([], rule_queries(context, True))
        context['learning']['turn_request']['text'] = '查询笔记和以前的通分记录'
        self.assertEqual(2, len(rule_queries(context, True)))
        self.assertEqual(1, len(rule_queries(context, False)))

    def test_all_handwritten_final_fixtures_obey_actual_work_kind_contract(self):
        from study.diagnosis import validate_analysis
        from study.context import validate_coach
        for case in load_cases():
            for turn in case['turns']:
                context = model_context(case, turn, task_for_turn(None, case, turn), 'A')
                envelope = handwritten_response(context, turn['operation'], case, 'A', {'messages': []})
                result = json.loads(envelope['choices'][0]['message']['content'])['result']
                if turn['operation'] == 'coach': validate_coach(result)
                else: validate_analysis(result, student_work=turn['student_work'], work_kind=turn['work_kind'])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / 'run'

    def test_default_preview_is_zero_calls_and_blank_scores(self):
        with patch('study.service.PhotoTransport.send', side_effect=AssertionError('network')):
            manifest = freeze(self.directory)
            status = report(self.directory)
            self.assertFalse(manifest['real_api_execution_enabled'])
            self.assertEqual('preview', status['mode'])
            self.assertEqual(0, status['real_api_calls'])
            self.assertEqual(0, status['arms']['A']['http_attempts'])
            summary = score_report(self.directory)['human_scoring']
            self.assertEqual(0, summary['rows_with_any_score'])
            self.assertIsNone(summary['dimensions']['math_steps']['mean']['A'])
            self.assertIsNone(summary['scoring_coverage'])

    def test_existing_output_and_symlink_are_not_overwritten(self):
        freeze(self.directory)
        before = (self.directory / 'manifest.json').read_bytes()
        with self.assertRaises(AuditError): freeze(self.directory)
        link = self.root / 'link'
        link.symlink_to(self.directory)
        with self.assertRaises(AuditError): freeze(link)
        with self.assertRaises(AuditError): verify(link)
        self.assertEqual(before, (self.directory / 'manifest.json').read_bytes())

    def test_plan_code_asset_and_config_changes_refuse_execution(self):
        freeze(self.directory)
        with patch('study.evaluation.asset_hashes', return_value={}), self.assertRaises(AuditError): verify(self.directory)
        with patch('study.evaluation.default_config', return_value=replace(default_config(), temperature=0.9)), self.assertRaises(AuditError): verify(self.directory)
        with patch('study.evaluation.source_snapshot', return_value={'source_hashes': {}}), self.assertRaises(AuditError): verify(self.directory)
        data = read_json(self.directory / 'manifest.json')
        data['split'] = 'reserved'
        write_json(self.directory / 'manifest.json', data)
        with self.assertRaises(AuditError): verify(self.directory)

    def test_scope_filters_old_disabled_and_unrelated_histories_without_writing(self):
        case = load_cases()[1]
        scope = seed_scope(self.root / 'scope', case, stamp())
        before = {p: p.read_bytes() for p in (self.root / 'scope').rglob('*.json')}
        result = scope.call('get_review_history', {'topic': '通分', 'limit': 3}, 3)
        self.assertEqual(1, len(result['sources']))
        self.assertEqual(digest([case['case_id'], 'related'])[:32], result['sources'][0]['record_id'])
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / 'scope').rglob('*.json')})

    def test_remote_transport_and_nonlocal_credentials_are_rejected(self):
        row = {'row_id': 'x', 'requests': []}
        class Remote: chat_url = 'https://api.deepseek.com/chat/completions'
        with self.assertRaises(AuditError): RecordingLocalTransport(Remote(), self.root, row, 1)
        with LocalModelServer() as server:
            t = RecordingLocalTransport(server, self.root, row, 1)
            with self.assertRaises(AuditError): t.send({}, 'not-the-local-key', 1, {})
            self.assertEqual([], server.requests)

    def test_no_paid_run_cli_and_status_never_executes(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main(['run', '--output', str(self.directory)])
        with redirect_stdout(io.StringIO()): self.assertEqual(0, main(['preview', '--output', str(self.directory)]))
        with patch('study.service.PhotoTransport.send', side_effect=AssertionError('network')):
            with redirect_stdout(io.StringIO()): self.assertEqual(0, main(['status', '--output', str(self.directory)]))

    def test_exact_payload_is_reserved_before_http_and_cap_blocks_another(self):
        (self.root / 'rows').mkdir()
        row = {'row_id': 'reservation', 'requests': []}
        with LocalModelServer() as server:
            transport = RecordingLocalTransport(server, self.root, row, 1)
            def inspect(payload, key, timeout, record):
                reserved = read_json(self.root / 'rows/reservation.json')['requests'][0]
                self.assertEqual('reserved', reserved['status'])
                self.assertEqual(payload, reserved['payload'])
                record.update(attempted_requests=1, http_status=200)
                return {}
            with patch.object(transport.base, 'send', side_effect=inspect):
                transport.send({'fixture': True}, 'local-test-key', 1, {})
            with self.assertRaises(AuditError): transport.send({}, 'local-test-key', 1, {})
            self.assertEqual(1, len(row['requests']))

    def test_conversation_failure_blocks_followups_and_survives_reopen(self):
        case = deepcopy(load_cases()[18])
        case['turns'].append({**case['turns'][0], 'operation': 'coach', 'intent': 'hint'})
        case['human_only']['turn_checks'].append('should remain blocked')
        with patch('study.evaluation.selected_cases', return_value=[case]): freeze(self.directory)
        status = run_local(self.directory)
        for arm in ('A', 'B'):
            self.assertEqual(1, status['arms'][arm]['failed'])
            self.assertEqual(1, status['arms'][arm]['blocked'])
            self.assertEqual([], read_json(self.directory / 'rows' / f'e19-{arm}-t02.json')['requests'])
        self.assertEqual(status, report(self.directory))

    def test_both_arms_reject_same_unauthorized_output_and_fabricated_citation(self):
        from study.agent import AgentStudyService
        case = load_cases()[0]
        turn = case['turns'][0]
        with LocalModelServer() as server:
            for arm in ('A', 'B'):
                scope = seed_scope(self.root / arm, case, stamp())
                context = model_context(case, turn, task_for_turn(None, case, turn), arm)
                for problem in ('extra_field', 'fabricated_source'):
                    envelope = handwritten_response(context, 'analyze', case, arm, {'messages': []})
                    value = json.loads(envelope['choices'][0]['message']['content'])
                    if problem == 'extra_field': value['result']['mastered'] = True
                    else: value['citations'] = [{'source_id': 'note:invented', 'quote': 'false source'}]
                    envelope['choices'][0]['message']['content'] = json.dumps(value)
                    server.body = envelope
                    kwargs = dict(scope=scope, transport=PhotoTransport(server.chat_url))
                    service = (FixedWorkflowService(default_config(), 'local-test-key', **kwargs) if arm == 'A' else
                               AgentStudyService(default_config(), 'local-test-key', audit_dir=self.root / 'logs', **kwargs))
                    with self.assertRaises(ValueError): service.call('analyze', ANALYSIS_PROMPT, context)
                    self.assertFalse(scope.history_dir.exists())


class RehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name) / 'run'
        # Representative conversations and error paths; the terminal demo runs the full suite.
        subset = [c for c in load_cases() if c['case_id'] in ('e01', 'e02', 'e08', 'e19', 'e26', 'e30')]
        with patch('study.evaluation.selected_cases', return_value=subset): cls.manifest = freeze(cls.directory, 'all')
        cls.status = run_local(cls.directory)

    @classmethod
    def tearDownClass(cls): cls.temporary.cleanup()

    def row(self, row_id): return read_json(self.directory / 'rows' / (row_id + '.json'))

    def test_full_actual_payloads_responses_tools_are_auditable(self):
        row = self.row('e02-B-t01')
        self.assertEqual('completed', row['status'])
        self.assertEqual(2, len(row['requests']))
        self.assertEqual('get_review_history', row['trace']['tool_calls'][0]['name'])
        self.assertEqual(1, len(row['trace']['sources']))
        for request in row['requests']:
            self.assertEqual(digest(request['payload']), request['request_hash'])
            self.assertNotIn('human_only', json.dumps(request['payload']))
            self.assertNotIn('local-test-key', json.dumps(request['payload']))
        self.assertEqual(0, row['notebook_saves'])
        self.assertIsNone(row['trace']['decision'])

    def test_same_config_and_confirmed_text_but_recorded_information_difference(self):
        a, b = self.row('e01-A-t01'), self.row('e01-B-t01')
        self.assertEqual(a['input_context'], b['input_context'])
        for field in ('model', 'temperature', 'thinking', 'max_tokens'):
            self.assertEqual(a['requests'][0]['payload'][field], b['requests'][0]['payload'][field])
        self.assertEqual(1, len(self.row('e08-A-t03')['input_context']['learning']['recent_dialogue']))
        self.assertEqual(2, len(self.row('e08-B-t03')['input_context']['learning']['recent_dialogue']))

    def test_temporary_preferences_expire_selection_does_not_complete_switch_resets(self):
        for arm in ('A', 'B'):
            temporary = self.row(f'e08-{arm}-t02')['input_context']['learning']
            next_turn = self.row(f'e08-{arm}-t03')['input_context']['learning']
            switched = self.row(f'e08-{arm}-t04')['input_context']['learning']
            self.assertEqual('detailed', temporary['effective_settings']['presentation_density'])
            self.assertEqual('brief', next_turn['effective_settings']['presentation_density'])
            self.assertEqual('next_step_selected', next_turn['task']['status'])
            self.assertEqual([], next_turn['task']['completed_steps'])
            self.assertEqual([], switched['recent_dialogue'])
            self.assertEqual('', switched['task']['selected_next_step'])

    def test_failures_counted_and_safety_faults_do_not_save_or_retry(self):
        for arm in ('A', 'B'):
            self.assertEqual('failed', self.row(f'e19-{arm}-t01')['status'])
        self.assertEqual('tool_not_allowed', self.row('e26-B-t01')['error_code'])
        self.assertEqual(1, self.status['arms']['A']['failed'])
        self.assertEqual(2, self.status['arms']['B']['failed'])
        self.assertEqual(0, self.status['real_api_calls'])
        before = self.row('e19-B-t01')
        with self.assertRaises(FileExistsError): run_local(self.directory)
        self.assertEqual(before, self.row('e19-B-t01'))

    def test_no_result_is_recorded_and_not_fabricated_into_sources(self):
        row = self.row('e30-B-t01')
        self.assertEqual('no_results', row['trace']['tool_calls'][0]['status'])
        self.assertEqual({}, row['trace']['sources'])

    def test_reopened_report_has_unknown_usage_and_blank_quality(self):
        reopened = report(self.directory)
        self.assertEqual(self.status, reopened)
        self.assertFalse(reopened['arms']['B']['token_usage_complete'])
        self.assertIsNone(reopened['actual_cost_cny'])
        scored = score_report(self.directory)['human_scoring']
        self.assertEqual(0, scored['rows_with_any_score'])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'scores'
        with patch('study.evaluation.selected_cases', return_value=[load_cases()[2]]):
            self.manifest = freeze(self.root)
        self.sheet = read_json(self.root / 'scores.json')
        self.results = {r['row_id']: {'status': 'completed', 'mode': 'real_api', 'trace': {'sources': {}}}
                        for r in self.manifest['rows']}

    def grade(self, index=0, value=2):
        row = self.sheet['rows'][index]
        row.update(reviewer='unit-test fictional grader', reviewer_role='developer_self', evidence='synthetic fixture quotation')
        row['scores']['math_steps'] = value

    def test_blank_and_partial_pair_not_counted_as_zero(self):
        self.grade()
        result = summarize(self.manifest, self.sheet, self.results)
        self.assertEqual(0, result['dimensions']['math_steps']['complete_pairs'])
        self.assertIsNone(result['dimensions']['math_steps']['paired_mean_B_minus_A'])
        self.grade(1, 1)
        result = summarize(self.manifest, self.sheet, self.results)
        self.assertEqual(1, result['dimensions']['math_steps']['complete_pairs'])
        self.assertEqual(-1, result['dimensions']['math_steps']['paired_mean_B_minus_A'])
        self.assertEqual(['developer_self'], result['reviewer_roles'])

    def test_failure_pending_or_local_cannot_be_graded(self):
        self.grade()
        row_id = self.sheet['rows'][0]['row_id']
        for status, mode in [('failed', 'real_api'), ('pending', 'real_api'), ('completed', 'local_http_test')]:
            self.results[row_id].update(status=status, mode=mode)
            with self.assertRaises(AuditError): summarize(self.manifest, self.sheet, self.results)

    def test_bool_range_unknown_dimensions_and_missing_evidence_rejected(self):
        original = deepcopy(self.sheet)
        for mutate in [lambda r: r['scores'].update(math_steps=True), lambda r: r['scores'].update(math_steps=3),
                       lambda r: r['scores'].update(invented=1), lambda r: r.update(evidence=None),
                       lambda r: r.update(reviewer_role=None)]:
            self.sheet = deepcopy(original)
            self.grade()
            mutate(self.sheet['rows'][0])
            with self.assertRaises(AuditError): summarize(self.manifest, self.sheet, self.results)

    def test_reference_ids_and_unsupported_claims_cannot_be_changed(self):
        for key in ('reference', 'row_id'):
            sheet = deepcopy(self.sheet)
            sheet['rows'][0][key] = 'changed'
            with self.assertRaises(AuditError): summarize(self.manifest, sheet, self.results)
        self.sheet['rows'][0]['unsupported_inference'] = True
        with self.assertRaises(AuditError): summarize(self.manifest, self.sheet, self.results)

    def test_inapplicable_source_and_history_scores_rejected(self):
        self.grade()
        for dimension in ('history_use', 'source_support'):
            self.sheet['rows'][0]['scores'][dimension] = 2
            with self.assertRaises(AuditError): summarize(self.manifest, self.sheet, self.results)
            self.sheet['rows'][0]['scores'][dimension] = None

    def test_failures_remain_in_total_denominator(self):
        self.grade()
        self.results[self.sheet['rows'][1]['row_id']]['status'] = 'failed'
        result = summarize(self.manifest, self.sheet, self.results)
        self.assertEqual(2, result['planned_rows'])
        self.assertEqual(1, result['real_completed_rows'])
        self.assertEqual(1, result['rows_with_any_score'])
        self.assertEqual(0, result['dimensions']['math_steps']['complete_pairs'])


if __name__ == '__main__': unittest.main()
