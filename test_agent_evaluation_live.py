"""Audited A/B runner. Every HTTP call is loopback; real-mode branches use explicit stubs."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from http_test_support import LocalModelServer
from study.evaluation_live import (DEFAULT_CASES, MAX_REQUEST_BYTES, CountedTransport, EvaluationPlan,
    authorize, checked_pricing, create, estimate, execute, input_hashes, money, preflight, status, verify)
from study.evaluation_scoring import score_report
from study.run_audit import AuditError, digest, read_json, write_json
from study.service import PhotoTransport
from study.smoke import ROOT, default_config, source_snapshot
from tools.evaluate_agent_live import local_response, main

FAKE_KEY = 'fixture-credential-must-never-be-recorded'


class PriceTests(unittest.TestCase):
    def test_pricing_is_peak_cny_and_estimate_not_actual_bill(self):
        price = checked_pricing(default_config(), today=date(2026, 9, 24))
        self.assertEqual(Decimal('0.162816'), estimate(65024, 4096, price))
        self.assertEqual(Decimal('1.628160'), 10 * estimate(65024, 4096, price))
        self.assertEqual('CNY', price['currency'])

    def test_stale_and_future_price_dates_fail(self):
        for day in (date(2026, 9, 23), date(2026, 10, 2)):
            with self.assertRaises(AuditError): checked_pricing(default_config(), today=day)

    def test_budget_rejects_nan_infinity_bool_float_and_nonpositive(self):
        for value in (True, 2.0, 'NaN', 'Infinity', '-1', '0', '1001', 'garbage'):
            with self.subTest(value=value), self.assertRaises(AuditError): money(value)
        self.assertEqual(Decimal('2.00'), money('2.00'))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.snapshot = source_snapshot()
        self.snapshot.update(commit='frozen-unit-test-commit', dirty=False)
        self.enterContext(patch('study.evaluation_live.source_snapshot', return_value=self.snapshot))
        self.enterContext(patch('study.smoke.source_snapshot', return_value=self.snapshot))
        # Tests remain offline and deterministic after the real quote expires.
        self.price = checked_pricing(default_config(), today=date(2026, 9, 24))
        self.enterContext(patch('study.evaluation_live.checked_pricing', return_value=self.price))
        self.server = self.enterContext(LocalModelServer())
        self.index = 0

    def plan(self, mode='local_http_test', cases=DEFAULT_CASES, budget='2'):
        self.index += 1
        return create(self.root / f'plan-{self.index}', mode=mode, case_ids=cases, budget_cny=budget)

    def approvals(self, plan):
        return {'key': FAKE_KEY, 'confirm_plan': plan.manifest['plan_id'],
                'max_requests': plan.manifest['max_model_requests'], 'budget_cny': plan.manifest['budget_cny']}

    def run_local(self, plan):
        self.server.body = local_response(plan)
        return execute(plan, server=self.server)

    def fake_real_response(self, plan, alter=None):
        fixture = local_response(plan)
        def send(payload, key, timeout, record):
            running = next(plan.row(r) for r in plan.rows if plan.row(r) and plan.row(r)['status'] == 'running')
            self.assertEqual('reserved', running['requests'][-1]['status'])
            self.assertGreater(Decimal(status(plan)['reserved_estimate_cny']), 0)
            record.update(attempted_requests=1, http_status=200)
            response = fixture(payload)
            response['usage'] = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
            response['choices'][0]['message']['reasoning_content'] = 'PRIVATE_REASONING_MUST_NOT_BE_RECORDED'
            if alter: alter(response)
            return response
        return send

    def file_bytes(self, folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def test_preview_freezes_scope_and_no_network_or_approval(self):
        with patch.object(PhotoTransport, 'send', side_effect=AssertionError('must not call')):
            plan = self.plan('real_api')
        self.assertEqual(4, len(plan.rows))
        self.assertEqual(10, status(plan)['max_model_requests'])
        self.assertEqual('1.628160', plan.manifest['maximum_reserved_estimate_cny'])
        self.assertEqual('preview', status(plan)['mode'])
        self.assertEqual(0, status(plan)['reserved_model_requests'])
        self.assertFalse((plan.directory / 'approval.json').exists())
        self.assertEqual([], self.server.requests)
        for path in (plan.directory / 'previews').glob('*.json'):
            raw = path.read_text()
            self.assertNotIn('human_only', raw)
            self.assertNotIn('reference', raw)
        self.assertIsNone(score_report(plan.directory)['human_scoring']['scoring_coverage'])

    def test_unknown_duplicate_reserved_cases_and_insufficient_budget_do_not_create(self):
        for cases, budget in ((('e03', 'e03'), '2'), (('e21',), '2'), (('unknown',), '2'), ((), '2'), (DEFAULT_CASES, '1')):
            path = self.root / 'bad'
            with self.assertRaises(AuditError): create(path, case_ids=cases, budget_cny=budget)
            self.assertFalse(path.exists())

    def test_existing_outputs_and_symlink_inputs_rejected(self):
        plan = self.plan()
        with self.assertRaises(AuditError): create(plan.directory)
        link = plan.directory / 'scopes' / 'external'
        link.symlink_to(ROOT / 'notes', target_is_directory=True)
        with self.assertRaises(AuditError): verify(plan)
        self.assertEqual([], self.server.requests)

    def test_exact_approval_and_key_before_reservations(self):
        plan = self.plan('real_api')
        good = self.approvals(plan)
        with patch.object(PhotoTransport, 'send', side_effect=AssertionError('must not call')):
            for change in ({'confirm_plan': None}, {'confirm_plan': 'wrong'}, {'max_requests': True},
                           {'max_requests': 9}, {'max_requests': 11}, {'budget_cny': '1'}, {'budget_cny': '3'},
                           {'budget_cny': None}, {'key': ''}, {'key': '中文'}, {'server': self.server}):
                with self.subTest(change=change), self.assertRaises(ValueError): execute(plan, **{**good, **change})
        self.assertFalse((plan.directory / 'approval.json').exists())
        self.assertFalse((plan.directory / 'execution.json').exists())
        self.assertEqual(0, status(plan)['reserved_model_requests'])

    def test_dirty_changed_commit_config_prices_and_assets_refuse_live(self):
        plan = self.plan('real_api')
        for snapshot in ({**self.snapshot, 'dirty': True}, {**self.snapshot, 'commit': 'different'}):
            with patch('study.evaluation_live.source_snapshot', return_value=snapshot), self.assertRaises(AuditError):
                execute(plan, **self.approvals(plan))
        with patch('study.evaluation_live.asset_hashes', return_value={}), self.assertRaises(AuditError): verify(plan)
        with patch('study.evaluation_live.checked_pricing', return_value={**self.price, 'peak_output_per_million': '9'}), self.assertRaises(AuditError): verify(plan)
        path = next((plan.directory / 'scopes').rglob('M-F01.json'))
        path.write_text(path.read_text() + ' ')
        with self.assertRaises(AuditError): verify(plan)
        self.assertEqual(0, status(plan)['reserved_model_requests'])

    def test_expired_plan_and_changed_limits_rejected_even_with_new_hash(self):
        plan = self.plan('real_api')
        original = deepcopy(plan.manifest)
        for change in ({'created_at': '2020-01-01T00:00:00+00:00'}, {'max_model_requests': 11}):
            value = {**original, **change}
            value['plan_id'] = digest({k: v for k, v in value.items() if k != 'plan_id'})
            write_json(plan.directory / 'manifest.json', value)
            with self.assertRaises(AuditError): verify(EvaluationPlan(plan.directory), live=True)

    def test_model_result_not_accepted_and_same_sources_for_two_arms(self):
        plan = self.plan()
        inputs = input_hashes(plan.directory)
        state = self.run_local(plan)
        self.assertEqual('completed', state['execution_status'])
        self.assertEqual(5, state['reserved_model_requests'])
        self.assertEqual(5, len(self.server.requests))
        self.assertEqual(0, state['notebook_saves'])
        self.assertEqual(0, state['known_real_api_attempts'])
        self.assertIsNone(state['actual_cost_cny'])
        self.assertEqual(inputs, input_hashes(plan.directory))
        a, b = plan.row('e12-A-t01'), plan.row('e12-B-t01')
        self.assertEqual(a['input_context'], b['input_context'])
        self.assertEqual(a['trace']['sources'], b['trace']['sources'])
        self.assertIsNone(b['trace']['decision'])

    def test_completed_repeat_sends_nothing_and_does_not_change_records(self):
        plan = self.plan()
        state = self.run_local(plan)
        before = self.file_bytes(plan.directory)
        self.assertEqual(state, execute(EvaluationPlan(plan.directory), server=self.server))
        self.assertEqual(before, self.file_bytes(plan.directory))
        self.assertEqual(5, len(self.server.requests))

    def test_failure_stops_entire_plan_without_retry_or_skip(self):
        plan = self.plan()
        self.server.body = {'object': 'chat.completion', 'choices': []}
        state = execute(plan, server=self.server)
        self.assertEqual('stopped', state['execution_status'])
        self.assertEqual(1, state['reserved_model_requests'])
        self.assertEqual(3, sum(s['pending'] for s in state['arms'].values()))
        before = self.file_bytes(plan.directory)
        with self.assertRaises(AuditError): execute(plan, server=self.server)
        self.assertEqual(before, self.file_bytes(plan.directory))
        self.assertEqual(1, len(self.server.requests))

    def test_tool_failure_before_model_stops_without_spending_request_slot(self):
        plan = self.plan(cases=('e19', 'e20'))
        state = self.run_local(plan)
        self.assertEqual('stopped', state['execution_status'])
        self.assertEqual(0, state['reserved_model_requests'])
        self.assertEqual('tool_failed', state['failures'][0]['error_code'])
        self.assertEqual([], self.server.requests)

    def test_real_mode_stub_reserves_before_send_and_keeps_bill_unknown(self):
        plan = self.plan('real_api')
        with patch.object(PhotoTransport, 'send', side_effect=self.fake_real_response(plan)):
            state = execute(plan, **self.approvals(plan))
        self.assertEqual(5, state['known_real_api_attempts'])
        self.assertEqual(Decimal('0.003000'), Decimal(state['provider_usage_peak_estimate_cny']))
        self.assertIsNone(state['actual_cost_cny'])
        self.assertTrue(state['usage_complete'])
        self.assertFalse(state['real_api_calls_unknown'])
        self.assertEqual([], self.server.requests)
        raw = json.dumps({n: v.decode() for n, v in self.file_bytes(plan.directory).items()})
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn('PRIVATE_REASONING_MUST_NOT_BE_RECORDED', raw)
        self.assertIn('explicit_cli_confirmation', raw)
        self.assertEqual(4, score_report(plan.directory)['human_scoring']['real_completed_rows'])
        self.assertEqual(0, score_report(plan.directory)['human_scoring']['rows_with_any_score'])

    def test_missing_usage_is_unknown_and_reserved_estimate_is_not_refunded(self):
        plan = self.plan('real_api')
        with patch.object(PhotoTransport, 'send', side_effect=self.fake_real_response(plan, lambda r: r.pop('usage'))):
            state = execute(plan, **self.approvals(plan))
        self.assertFalse(state['usage_complete'])
        self.assertIsNone(state['usage_totals_reported'])
        self.assertIsNone(state['provider_usage_peak_estimate_cny'])
        self.assertGreater(Decimal(state['reserved_estimate_cny']), 0)

    def test_provider_usage_exceeding_estimate_stops_after_paid_attempt(self):
        plan = self.plan('real_api')
        def oversized(response): response['usage'] = {'prompt_tokens': 999999, 'completion_tokens': 50, 'total_tokens': 1000049}
        with patch.object(PhotoTransport, 'send', side_effect=self.fake_real_response(plan, oversized)):
            state = execute(plan, **self.approvals(plan))
        self.assertEqual(1, state['known_real_api_attempts'])
        self.assertEqual('usage_exceeds_reservation', state['failures'][0]['error_code'])
        self.assertEqual('stopped', state['execution_status'])

    def test_interrupted_attempt_is_unknown_and_restart_does_not_resend(self):
        plan = self.plan('real_api')
        def interrupt(payload, key, timeout, record):
            record['attempted_requests'] = 1
            raise KeyboardInterrupt()
        with patch.object(PhotoTransport, 'send', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            execute(plan, **self.approvals(plan))
        reopened = EvaluationPlan(plan.directory)
        self.assertTrue(status(reopened)['real_api_calls_unknown'])
        self.assertTrue(reopened.row('e03-A-t01')['trace']['model_calls'][0]['completion_unknown'])
        self.assertEqual('running', status(reopened)['execution_status'])
        with self.assertRaises(AuditError): execute(reopened, **self.approvals(reopened))
        self.assertEqual(1, status(reopened)['reserved_model_requests'])

    def test_crash_without_started_event_still_consumes_reservation(self):
        plan = self.plan('real_api')
        with patch.object(PhotoTransport, 'send', side_effect=OSError('simulated transport loss')):
            state = execute(plan, **self.approvals(plan))
        self.assertEqual(0, state['known_real_api_attempts'])
        self.assertEqual(1, state['reserved_model_requests'])
        self.assertTrue(state['real_api_calls_unknown'])

    def test_invalid_json_diagnostic_is_preserved_without_reasoning_or_key(self):
        plan = self.plan('real_api')
        def invalid(response): response['choices'][0]['message']['content'] = '{broken JSON ' + FAKE_KEY
        with patch.object(PhotoTransport, 'send', side_effect=self.fake_real_response(plan, invalid)):
            state = execute(plan, **self.approvals(plan))
        self.assertEqual('stopped', state['execution_status'])
        raw = plan.path('e03-A-t01').read_text()
        self.assertIn('broken JSON', raw)
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn('PRIVATE_REASONING', raw)
        self.assertIsNone(plan.row('e03-A-t01')['result'])

    def test_payload_budget_context_leak_and_changed_parameters_block_before_http(self):
        original = CountedTransport.send
        for modify in (
            lambda p: p.update(max_tokens=4097),
            lambda p: p['messages'][0].update(content='x' * (MAX_REQUEST_BYTES + 1)),
            lambda p: p['messages'][1]['content'][0].update(text=p['messages'][1]['content'][0]['text'].replace('"attached_images": []', '"attached_images": [], "human_only": "gold"'))):
            plan = self.plan()
            def altered(transport, payload, key, timeout, record):
                modify(payload)
                return original(transport, payload, key, timeout, record)
            with patch.object(CountedTransport, 'send', altered):
                state = execute(plan, server=self.server)
            self.assertEqual('stopped', state['execution_status'])
            self.assertEqual(0, state['reserved_model_requests'])
        self.assertEqual([], self.server.requests)

    def test_per_round_request_budget_cannot_be_bypassed(self):
        original = CountedTransport.send
        plan = self.plan()
        self.server.body = local_response(plan)
        def duplicate(transport, payload, key, timeout, record):
            original(transport, payload, key, timeout, record)
            return original(transport, payload, key, timeout, record)
        with patch.object(CountedTransport, 'send', duplicate): state = execute(plan, server=self.server)
        self.assertEqual('request_budget_exhausted', state['failures'][0]['error_code'])
        self.assertEqual(1, len(self.server.requests))

    def test_total_reservation_count_and_money_guard_block_sending(self):
        plan = self.plan()
        original = status
        for field, value in (('reserved_model_requests', 10), ('reserved_estimate_cny', '2')):
            # Patch only the transport's budget observation, not persisted execution state.
            import study.evaluation_live as module
            send = CountedTransport.send
            def with_exhausted(transport, payload, key, timeout, record):
                with patch.object(module, 'status', side_effect=lambda p: {**original(p), field: value}):
                    return send(transport, payload, key, timeout, record)
            candidate = self.plan()
            with patch.object(CountedTransport, 'send', with_exhausted): result = execute(candidate, server=self.server)
            self.assertEqual(0, result['reserved_model_requests'])
        self.assertEqual([], self.server.requests)

    def test_multiturn_context_and_selected_step_are_not_completion(self):
        plan = self.plan(cases=('e01',), budget='3')
        state = self.run_local(plan)
        self.assertEqual('completed', state['execution_status'])
        self.assertEqual(6, state['reserved_model_requests'])
        for arm in ('A', 'B'):
            row = plan.row(f'e01-{arm}-t03')
            self.assertEqual('next_step_selected', row['task_before']['status'])
            self.assertEqual([], row['task_after']['completed_steps'])
        self.assertEqual(1, len(plan.row('e01-A-t03')['input_context']['learning']['recent_dialogue']))
        self.assertEqual(2, len(plan.row('e01-B-t03')['input_context']['learning']['recent_dialogue']))

    def test_missing_next_step_does_not_fabricate_a_user_selection(self):
        plan = self.plan(cases=('e01',), budget='3')
        fixture = local_response(plan)
        def no_next(payload):
            response = fixture(payload)
            value = json.loads(response['choices'][0]['message']['content'])
            if value['result']['schema_version'] == 1: value['result']['next_step'] = ''
            response['choices'][0]['message']['content'] = json.dumps(value)
            return response
        self.server.body = no_next
        state = execute(plan, server=self.server)
        self.assertEqual(2, state['reserved_model_requests'])
        self.assertEqual('missing_selected_next_step', state['failures'][0]['error_code'])

    def test_history_inputs_unchanged_and_scoring_rejects_local_results(self):
        plan = self.plan(cases=('e02',))
        before = input_hashes(plan.directory)
        self.run_local(plan)
        self.assertEqual(before, input_hashes(plan.directory))
        self.assertEqual(1, len(plan.row('e02-B-t01')['trace']['sources']))
        sheet = read_json(plan.directory / 'scores.json')
        sheet['rows'][0]['scores']['math_steps'] = 2
        write_json(plan.directory / 'scores.json', sheet)
        with self.assertRaises(AuditError): score_report(plan.directory)

    def test_ledger_mutation_is_detected(self):
        plan = self.plan()
        self.run_local(plan)
        row = plan.row('e03-A-t01')
        row['requests'][0]['reserved_estimate_cny'] = '0'
        write_json(plan.path(row['row_id']), row)
        with self.assertRaises(AuditError): status(EvaluationPlan(plan.directory))

    def test_new_process_can_read_stopped_state_without_model_or_key(self):
        plan = self.plan()
        self.server.body = {'object': 'chat.completion', 'choices': []}
        state = execute(plan, server=self.server)
        process = subprocess.run([sys.executable, '-B', '-m', 'tools.evaluate_agent_live', 'status', '--directory', str(plan.directory)],
                                 cwd=ROOT, capture_output=True, text=True, env={'PYTHONIOENCODING': 'utf-8'}, timeout=10)
        self.assertEqual(2, process.returncode)
        self.assertEqual(state, json.loads(process.stdout))
        self.assertEqual(1, len(self.server.requests))

    def test_no_interactive_key_prompt_without_valid_approval_or_tty(self):
        plan = self.plan('real_api')
        good = ['run', '--directory', str(plan.directory), '--confirm-plan', plan.manifest['plan_id'],
                '--max-model-requests', '10', '--budget-cny', '2']
        with redirect_stderr(io.StringIO()), patch('tools.evaluate_agent_live.getpass.getpass', side_effect=AssertionError('key prompt')):
            self.assertEqual(2, main(['run', '--directory', str(plan.directory)]))
            with patch('tools.evaluate_agent_live.sys.stdin.isatty', return_value=False):
                self.assertEqual(2, main(good))
        self.assertEqual([], self.server.requests)

    def test_completed_cli_repeat_does_not_prompt_for_key(self):
        plan = self.plan('real_api')
        with patch.object(PhotoTransport, 'send', side_effect=self.fake_real_response(plan)):
            execute(plan, **self.approvals(plan))
        with redirect_stdout(io.StringIO()), patch('tools.evaluate_agent_live.getpass.getpass', side_effect=AssertionError('key prompt')):
            self.assertEqual(0, main(['run', '--directory', str(plan.directory), '--confirm-plan', plan.manifest['plan_id'],
                                     '--max-model-requests', '10', '--budget-cny', '2']))

    def test_run_lock_refuses_concurrent_execution(self):
        plan = self.plan()
        with plan.locked(), self.assertRaises(AuditError):
            execute(EvaluationPlan(plan.directory), server=self.server)
        self.assertEqual([], self.server.requests)

    def test_reservation_write_failure_never_sends_http(self):
        plan = self.plan()
        put = plan.put
        def broken(row_id, value):
            if value['requests'] and value['requests'][-1]['status'] == 'reserved':
                raise OSError('simulated disk failure before send')
            return put(row_id, value)
        with patch.object(plan, 'put', side_effect=broken):
            state = execute(plan, server=self.server)
        self.assertEqual(0, state['reserved_model_requests'])
        self.assertEqual('stopped', state['execution_status'])
        self.assertEqual([], self.server.requests)

    def test_local_plan_cannot_be_upgraded_and_status_is_read_only(self):
        plan = self.plan()
        before = self.file_bytes(plan.directory)
        with self.assertRaises(AuditError): preflight(plan, plan.manifest['plan_id'], 10, '2')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(['status', '--directory', str(plan.directory)]))
        self.assertEqual(before, self.file_bytes(plan.directory))


if __name__ == '__main__': unittest.main()
