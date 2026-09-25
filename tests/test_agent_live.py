"""冻结联调、累计 HTTP 名额与失败停机；所有网络均为本机或 stub。"""
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from legacy.http_test_support import LocalModelServer
from study.agent_live import AgentPlan, CountedTransport, create, verify, authorize, execute, decide, MAX_REQUESTS,MAX_REQUEST_BYTES
from study.run_audit import AuditError,read_json,write_json,digest
from study.service import PhotoTransport
from study.smoke import source_snapshot
from study.notebook import Notebook
from tools.verify_agent_live import local_response,main


class LivePlanTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.snapshot=source_snapshot();self.snapshot.update(commit='frozen-test-commit',dirty=False)
        self.enterContext(patch('study.agent_live.source_snapshot',return_value=self.snapshot))
        self.enterContext(patch('study.smoke.source_snapshot',return_value=self.snapshot))
        self.server=self.enterContext(LocalModelServer())

    def plan(self,name='run',mode='local_http_test'):
        return create(self.root/name,mode)

    def run_local(self,plan):
        self.server.body=local_response(plan);return execute(plan,server=self.server)

    def bytes(self,folder):return {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def test_preview_is_zero_http_five_frozen_inputs_and_no_answer_leak(self):
        with patch.object(PhotoTransport,'send',side_effect=AssertionError('must not send')):plan=self.plan(mode='real_api')
        state=plan.status();self.assertEqual(0,state['reserved_model_requests']);self.assertEqual(5,state['counts']['pending'])
        self.assertEqual(10,state['max_model_requests']);self.assertEqual([],self.server.requests)
        self.assertFalse((plan.directory/'notebook').exists());self.assertFalse((plan.directory/'approval.json').exists())
        for row in plan.rows.values():
            payload=row['first_payload'];self.assertEqual('auto',payload['tool_choice'])
            self.assertNotIn('review_reference',json.dumps(payload));self.assertNotIn('local_fixture',json.dumps(payload))
            self.assertIn('2 次模型请求',payload['messages'][0]['content'])
            self.assertLess(len(json.dumps(payload,ensure_ascii=False).encode()),MAX_REQUEST_BYTES)
        self.assertEqual(['search_notes'],[t['function']['name'] for t in plan.rows['b02']['first_payload']['tools']])
        self.assertEqual(2,len(plan.rows['b04']['first_payload']['tools']))
        self.assertNotIn('1/2 + 1/3 = 2/5',json.dumps(plan.rows['b04']['first_payload']))
        with self.assertRaises(AuditError):create(plan.directory)

    def test_live_requires_exact_approval_key_clean_code_and_no_mock_server(self):
        plan=self.plan(mode='real_api');valid={'key':'fake-test-credential','confirm_plan':plan.manifest['plan_id'],
                                           'max_requests':10,'budget_cny':1}
        with patch.object(PhotoTransport,'send',side_effect=AssertionError('must not send')):
            for update in ({'confirm_plan':'wrong'},{'max_requests':11},{'max_requests':True},{'budget_cny':2},{'key':''},{'key':'mistyped key'},{'key':'中文'},{'server':self.server}):
                with self.subTest(update=update),self.assertRaises(ValueError):execute(plan,**{**valid,**update})
            with patch('study.agent_live.source_snapshot',return_value={**self.snapshot,'dirty':True}),self.assertRaises(AuditError):execute(plan,**valid)
        self.assertFalse((plan.directory/'approval.json').exists());self.assertEqual(0,plan.status()['reserved_model_requests'])

    def test_five_scenarios_share_actual_agent_loop_and_eight_http_calls(self):
        plan=self.plan();before={p:p.read_bytes() for p in (plan.directory/'inputs').rglob('*.json')}
        state=self.run_local(plan)
        self.assertEqual({'pending':0,'completed':4,'failed':1,'running':0},state['counts'])
        self.assertEqual(8,state['reserved_model_requests']);self.assertEqual(8,len(self.server.requests))
        self.assertEqual({'b01':'direct_observed','b02':'notes_observed','b03':'no_results_observed','b04':'history_observed','b05':'injected_tool_failure_observed'},state['observed_branches'])
        self.assertEqual(0,state['known_real_api_attempts']);self.assertEqual(0,state['notebook_saves'])
        self.assertIsNone(state['human_scores']);self.assertFalse((plan.directory/'notebook').exists())
        self.assertTrue(all(p.read_bytes()==content for p,content in before.items()))
        for row_id in plan.rows:
            for row,call in zip(plan.row(row_id)['requests'],plan.trace(row_id)['model_calls']):
                self.assertEqual(row['request_hash'],call['request_hash'])
                self.assertEqual(row['request_id'],call['request_id'])
        self.assertEqual('none',plan.row('b02')['requests'][1]['payload']['tool_choice'])

    def test_failure_refuses_repeat_or_skip_and_never_asks_key_again(self):
        plan=self.plan();self.run_local(plan);before=self.bytes(plan.directory)
        with self.assertRaises(AuditError):execute(AgentPlan(plan.directory),server=self.server)
        self.assertEqual(before,self.bytes(plan.directory));self.assertEqual(8,len(self.server.requests))

    def test_provider_index_handwritten_five_scenarios_keep_limits_and_diagnostics(self):
        plan=self.plan();reply=local_response(plan)
        def indexed(payload):
            value=reply(payload)
            for i,call in enumerate(value['choices'][0]['message'].get('tool_calls') or []):call['index']=i
            return value
        self.server.body=indexed;state=execute(plan,server=self.server)
        self.assertEqual({'pending':0,'completed':4,'failed':1,'running':0},state['counts'])
        self.assertEqual(8,len(self.server.requests));self.assertEqual(0,state['known_real_api_attempts'])
        self.assertEqual('injected_tool_failure_observed',state['observed_branches']['b05'])
        self.assertEqual('notes_observed',state['observed_branches']['b02'])
        diagnostic=plan.row('b02')['requests'][0]['tool_response_diagnostic']
        self.assertEqual(diagnostic,plan.trace('b02')['model_calls'][0]['tool_response_diagnostic'])
        self.assertEqual(0,diagnostic['calls'][0]['index']['value'])
        self.assertFalse((plan.directory/'notebook').exists())
        before=self.bytes(plan.directory)
        with self.assertRaises(AuditError):execute(AgentPlan(plan.directory),server=self.server)
        self.assertEqual(before,self.bytes(plan.directory));self.assertEqual(8,len(self.server.requests))

    def test_invalid_tool_shape_stops_and_preserves_private_diagnostic_for_review(self):
        plan=self.plan();reply=local_response(plan)
        def invalid(payload):
            value=reply(payload)
            for call in value['choices'][0]['message'].get('tool_calls') or []:
                call['unexpected']='MUST_NOT_LOG_UNKNOWN_VALUE'
            return value
        self.server.body=invalid;state=execute(plan,server=self.server)
        self.assertEqual({'pending':3,'completed':1,'failed':1,'running':0},state['counts'])
        self.assertEqual(2,len(self.server.requests));self.assertEqual('invalid_tool_call',plan.row('b02')['error_code'])
        diagnostic=plan.row('b02')['requests'][0]['tool_response_diagnostic']
        self.assertIn('unexpected',diagnostic['calls'][0]['keys'])
        self.assertNotIn('MUST_NOT_LOG_UNKNOWN_VALUE',json.dumps(self.bytes(plan.directory),default=str))
        self.assertEqual([],plan.trace('b02')['tool_calls']);self.assertFalse((plan.directory/'notebook').exists())

    def test_first_invalid_response_stops_all_remaining_cases(self):
        plan=self.plan();self.server.body={'object':'chat.completion','choices':[]}
        state=execute(plan,server=self.server)
        self.assertEqual(1,len(self.server.requests));self.assertEqual(4,state['counts']['pending'])
        self.assertEqual('unexpected_failure',state['observed_branches']['b01'])
        with self.assertRaises(AuditError):execute(plan,server=self.server)

    def test_model_not_using_tool_is_not_mislabeled_as_coverage(self):
        from tools.demo_study_agent import envelope
        from study.agent_live import ROOT
        replies=read_json(ROOT/'evaluation/study_smoke_local_responses.json')['responses'];plan=self.plan()
        def reply(payload):
            context=json.loads(payload['messages'][1]['content'][0]['text'])
            key='b04' if context['school_level']=='初中' else 'b02'
            return envelope(result=replies[key])
        self.server.body=reply;state=execute(plan,server=self.server)
        self.assertEqual(5,state['counts']['completed']);self.assertEqual(5,state['reserved_model_requests'])
        self.assertEqual('not_exercised',state['observed_branches']['b02'])
        self.assertEqual('not_exercised',state['observed_branches']['b05'])
        before=self.bytes(plan.directory);execute(AgentPlan(plan.directory),server=self.server)
        # 再次运行只刷新汇总，不产生模型调用或错题收藏。
        self.assertEqual(5,len(self.server.requests));self.assertFalse((plan.directory/'notebook').exists())
        self.assertEqual(before,self.bytes(plan.directory))

    def test_two_request_limit_applies_even_if_model_keeps_asking_tools(self):
        from tools.demo_study_agent import envelope
        plan=self.plan();self.server.body=lambda p:envelope(calls=[(str(len(p['messages'])),'search_notes',{'query':'通分'})])
        state=execute(plan,server=self.server)
        self.assertEqual(2,len(self.server.requests));self.assertEqual(2,state['reserved_model_requests'])
        self.assertEqual('tool_budget_exhausted',plan.row('b01')['error_code'])
        self.assertEqual(4,state['counts']['pending'])

    def test_changed_assets_code_or_plan_block_before_any_request(self):
        plan=self.plan();path=next((plan.directory/'inputs/b02/notes').glob('*.json'));old=path.read_bytes()
        path.write_text('{}')
        with self.assertRaises(AuditError):execute(plan,server=self.server)
        path.write_bytes(old)
        with patch('study.agent_live.source_snapshot',return_value={**self.snapshot,'commit':'changed'}),self.assertRaises(AuditError):execute(plan,server=self.server)
        manifest=read_json(plan.directory/'manifest.json');manifest['max_model_requests']=11
        write_json(plan.directory/'manifest.json',manifest)
        with self.assertRaises(AuditError):execute(plan,server=self.server)
        self.assertEqual([],self.server.requests)

    def test_symlink_and_unlisted_file_cannot_expand_sources(self):
        plan=self.plan();path=plan.directory/'inputs/b03/notes/extra.json';path.write_text('{}')
        with self.assertRaises(AuditError):verify(plan)
        path.unlink();path.symlink_to(plan.directory/'manifest.json')
        with self.assertRaises(AuditError):verify(plan)
        self.assertEqual([],self.server.requests)

    def test_mutation_during_response_stops_before_tools_or_second_request(self):
        from tools.demo_study_agent import envelope
        plan=self.plan()
        def reply(payload):
            (plan.directory/'inputs/b01/notes/extra.json').write_text('{}')
            return envelope(calls=[('a','search_notes',{'query':'通分'})])
        self.server.body=reply;state=execute(plan,server=self.server)
        self.assertEqual(1,state['reserved_model_requests']);self.assertEqual('sources_changed',plan.row('b01')['error_code'])
        self.assertEqual([],plan.trace('b01')['tool_calls'])

    def test_byte_limit_aborts_without_silently_truncating_context(self):
        plan=self.plan();self.server.body=local_response(plan)
        with patch('study.agent_live.byte_count',side_effect=lambda p:26_001 if len(p['messages'])>2 else 100):
            state=execute(plan,server=self.server)
        self.assertEqual(2,len(self.server.requests));self.assertEqual(2,state['reserved_model_requests'])
        self.assertEqual(1,len(plan.row('b02')['requests']));self.assertEqual(3,state['counts']['pending'])

    def test_global_reservation_limit_cannot_be_bypassed_by_new_row(self):
        plan=self.plan();payload=plan.rows['b01']['first_payload']
        with plan.locked():
            plan.put('b01',{'row_id':'b01','plan_id':plan.manifest['plan_id'],'status':'running','requests':[]})
            transport=CountedTransport(plan,'b01',PhotoTransport(self.server.chat_url))
            with patch.object(plan,'status',return_value={'reserved_model_requests':MAX_REQUESTS}),self.assertRaises(AuditError):
                transport.send(payload,'local-test-key',2,{'request_id':'x'})
        self.assertEqual([],self.server.requests);self.assertEqual([],plan.row('b01')['requests'])

    def test_real_mode_crash_is_unknown_and_resume_does_not_resend(self):
        plan=self.plan(mode='real_api');args={'key':'synthetic-hidden-key','confirm_plan':plan.manifest['plan_id'],'max_requests':10,'budget_cny':1}
        def interrupted(*args):
            self.assertEqual(1,plan.status()['reserved_model_requests'])
            self.assertTrue(plan.status()['real_api_calls_unknown'])
            raise KeyboardInterrupt
        with patch.object(PhotoTransport,'send',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):execute(plan,**args)
        state=AgentPlan(plan.directory).status();self.assertTrue(state['real_api_calls_unknown']);self.assertEqual(1,state['counts']['running'])
        with patch.object(PhotoTransport,'send',side_effect=AssertionError('must not retry')),self.assertRaises(AuditError):execute(AgentPlan(plan.directory),**args)
        self.assertNotIn(args['key'],json.dumps(self.bytes(plan.directory),default=str));self.assertEqual([],self.server.requests)

    def test_overlapping_process_is_rejected_before_http(self):
        plan=self.plan()
        with plan.locked(),self.assertRaises(AuditError):execute(AgentPlan(plan.directory),server=self.server)
        self.assertEqual([],self.server.requests)

    def test_invalid_json_keeps_redacted_diagnostic_without_reasoning(self):
        from tools.demo_study_agent import envelope
        plan=self.plan();reply=envelope(result={});message=reply['choices'][0]['message']
        message.update(content='{"bad":',reasoning_content='INTERNAL_REASONING_NOT_FOR_LOG')
        self.server.body=reply;execute(plan,server=self.server)
        self.assertEqual('{"bad":',plan.row('b01')['requests'][0]['diagnostic_content'])
        self.assertNotIn('INTERNAL_REASONING_NOT_FOR_LOG',json.dumps(self.bytes(plan.directory),default=str))

    def test_key_echo_is_not_in_diagnostic_or_private_logs(self):
        from tools.demo_study_agent import envelope
        plan=self.plan();reply=envelope(result={});reply['choices'][0]['message']['content']='local-test-key'
        self.server.body=reply;execute(plan,server=self.server)
        self.assertIsNone(plan.row('b01')['requests'][0]['diagnostic_content'])
        self.assertNotIn('local-test-key',json.dumps(self.bytes(plan.directory),default=str))

    def test_manual_decisions_restart_and_repeated_confirmation_do_not_call_model(self):
        plan=self.plan();self.run_local(plan)
        self.assertFalse((plan.directory/'notebook').exists());decide(plan,'b02','reject')
        self.assertFalse((plan.directory/'notebook').exists())
        decision=decide(plan,'b01','accept');book=Notebook(plan.directory/'notebook');before=self.bytes(plan.directory)
        self.assertEqual(decision,decide(AgentPlan(plan.directory),'b01','accept'))
        self.assertEqual(before,self.bytes(plan.directory));self.assertEqual(8,len(self.server.requests))
        entry=book.get(decision['entry_id']);self.assertEqual([],entry['reviews'])
        value=json.loads(subprocess.check_output([sys.executable,'-B','-c',
            'import json,sys;from study.notebook import Notebook;print(json.dumps(Notebook(sys.argv[1]).get(sys.argv[2])))',
            str(book.directory),entry['id']],text=True))
        self.assertEqual(entry,value);self.assertEqual(1,plan.status()['notebook_saves']);self.assertEqual(1,plan.status()['rejected'])
        with self.assertRaises(ValueError):decide(plan,'b02','accept')
        with self.assertRaises(ValueError):decide(plan,'b05','accept')

    def test_expired_tampered_or_changed_source_cannot_save(self):
        plan=self.plan();self.run_local(plan)
        log=plan.directory/'agent-runs'/(plan.trace_id('b01')+'.json');original=read_json(log)
        changed=deepcopy(original);changed['finished_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();write_json(log,changed)
        with self.assertRaises(ValueError):decide(plan,'b01','accept')
        changed=deepcopy(original);changed['final']['result']['answer']='altered';write_json(log,changed)
        with self.assertRaises(ValueError):decide(plan,'b01','accept')
        write_json(log,original);(plan.directory/'inputs/b01/notes/extra.json').write_text('{}')
        with self.assertRaises(ValueError):decide(plan,'b01','accept')
        self.assertFalse((plan.directory/'notebook').exists());self.assertEqual(8,len(self.server.requests))

    def test_saved_then_decision_failure_can_recover_without_duplicate(self):
        plan=self.plan();self.run_local(plan)
        with patch('study.agent_live.record_decision',side_effect=OSError('simulated')),self.assertRaises(OSError):decide(plan,'b01','accept')
        book=Notebook(plan.directory/'notebook');before=self.bytes(book.directory)
        decide(AgentPlan(plan.directory),'b01','accept');self.assertEqual(before,self.bytes(book.directory))
        self.assertEqual(1,len(book.list()[0]));self.assertEqual(8,len(self.server.requests))

    def test_cli_preflight_refuses_before_hidden_key_prompt_and_status_is_readonly(self):
        plan=self.plan(mode='real_api');base=['run','--directory',str(plan.directory)]
        before=self.bytes(plan.directory)
        with patch('tools.verify_agent_live.getpass.getpass',side_effect=AssertionError('must not ask')):
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(2,main(base))
                self.assertEqual(2,main(base+['--confirm-plan',plan.manifest['plan_id'],'--max-model-requests','11','--budget-cny','1']))
                with patch('tools.verify_agent_live.sys.stdin.isatty',return_value=False):
                    self.assertEqual(2,main(base+['--confirm-plan',plan.manifest['plan_id'],'--max-model-requests','10','--budget-cny','1']))
                self.assertEqual(0,main(['status','--directory',str(plan.directory)]))
        self.assertEqual(before,self.bytes(plan.directory));self.assertEqual([],self.server.requests)

    def test_local_plan_cannot_be_executed_as_real_from_cli(self):
        plan=self.plan()
        with patch('tools.verify_agent_live.getpass.getpass',side_effect=AssertionError('must not ask')):
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(2,main(['run','--directory',str(plan.directory),'--confirm-plan',plan.manifest['plan_id'],
                                        '--max-model-requests','10','--budget-cny','1']))
        self.assertEqual([],self.server.requests)

    def test_cancel_hidden_key_prompt_does_not_create_approval_or_send(self):
        plan=self.plan(mode='real_api')
        with patch('tools.verify_agent_live.sys.stdin.isatty',return_value=True),patch('tools.verify_agent_live.getpass.getpass',side_effect=KeyboardInterrupt):
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(130,main(['run','--directory',str(plan.directory),'--confirm-plan',plan.manifest['plan_id'],
                                         '--max-model-requests','10','--budget-cny','1']))
        self.assertFalse((plan.directory/'approval.json').exists());self.assertEqual([],self.server.requests)


if __name__=='__main__':unittest.main()
