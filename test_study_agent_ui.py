"""主页面实际入口的 Agent 循环；HTTP 回复手写，不连接真实模型。"""
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import patch
from uuid import uuid4
import unittest

from streamlit.testing.v1 import AppTest
import test_study as fixtures
from test_study_agent import final, note_call, calls, history_entry
from http_test_support import LocalModelServer
from study.agent_audit import AgentAudit
from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
from study.notebook import Notebook, make_entry
from study.service import PhotoTransport, StudyService
from study.smoke import default_config


class AgentAppTests(unittest.TestCase):
    def setUp(self):
        fixtures.PhotoAppTests.setUp(self)
        self.server=self.enterContext(LocalModelServer())
        send=PhotoTransport.send
        def local_only(transport,*args,**kwargs):
            self.assertEqual('local_http_test',transport.kind,'UI test must never send to a real model')
            return send(transport,*args,**kwargs)
        self.enterContext(patch.object(PhotoTransport,'send',local_only))
        self.enterContext(patch('study.ui.service',side_effect=lambda:StudyService(default_config(),
            'local-test-key',PhotoTransport(self.server.chat_url))))
        self.book=Notebook(self.folder/'notebook');self.audit=AgentAudit(self.folder/'agent-runs')

    def prepare(self,history=False):
        self.app.checkbox(key='study_agent_enabled').check().run()
        if history:self.app.checkbox(key='study_agent_history').check().run()
        self.app.text_area(key='photo_question').set_value(QUESTION).run()
        self.app.selectbox(key='photo_level').select('初中').run()
        self.app.text_area(key='photo_work').set_value(STUDENT_WORK).run()
        self.app.radio(key='photo_work_kind').set_value('steps').run()
        self.assertTrue(fixtures.button(self.app,'分析这道题').disabled)
        self.app.checkbox(key='photo_confirmed').check().run()

    def click(self,label):
        fixtures.button(self.app,label).click().run()
        self.assertFalse(self.app.exception)

    def latest(self):
        return self.audit.read(self.app.session_state.study_agent_last_run['run_id'])

    def test_default_off_and_enabling_does_not_request_or_prefetch(self):
        self.assertFalse(self.app.checkbox(key='study_agent_enabled').value)
        self.assertTrue(self.app.checkbox(key='study_agent_history').disabled)
        self.prepare();self.assertEqual([],self.server.requests)
        self.assertFalse(self.book.directory.exists());self.assertFalse(self.audit.directory.exists())

    def test_actual_page_tools_rerun_and_rejection_do_not_save_or_repeat(self):
        self.prepare();responses=iter([note_call(),final()]);self.server.body=lambda p:next(responses)
        self.click('分析这道题');run=self.latest()
        self.assertEqual('success',run['status']);self.assertEqual(1,len(run['tool_calls']))
        self.assertFalse(self.book.directory.exists());self.assertEqual(2,len(self.server.requests))
        self.app.run();self.click('分析这道题');self.assertEqual(2,len(self.server.requests))
        self.assertTrue(any(e.label=='本次资料查询记录' for e in self.app.expander))
        self.click('放弃这次分析');self.app.run()
        self.assertIsNone(self.app.session_state.photo_analysis)
        self.assertEqual('reject',self.latest()['decision']['action'])
        self.assertFalse(self.book.directory.exists())
        self.click('分析这道题');self.assertEqual(2,len(self.server.requests))
        self.assertIsNone(self.app.session_state.photo_analysis)

    def test_accept_is_persisted_once_and_new_page_can_restore(self):
        self.prepare();self.server.body=final();self.click('分析这道题')
        self.assertFalse(self.book.directory.exists());self.click('确认加入错题本');self.app.run()
        entries,_=self.book.list();self.assertEqual(1,len(entries));self.assertEqual(ANALYSIS,entries[0]['analysis'])
        self.assertEqual([],entries[0]['reviews']);self.assertEqual('accept',self.latest()['decision']['action'])
        self.assertEqual(entries[0]['id'],self.latest()['decision']['entry_id']);self.assertEqual(1,len(self.server.requests))
        before=self.book.path(entries[0]['id']).read_bytes()
        fresh=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        fresh.radio(key='study_view').set_value('错题本').run()
        self.assertFalse(fresh.exception);self.assertEqual(before,self.book.path(entries[0]['id']).read_bytes())
        self.assertFalse(fresh.checkbox(key='study_agent_history').value)

    def test_disabled_history_absent_from_payload_and_policy_change_invalidates_candidate(self):
        private=history_entry(self.book,topic='方程',work='PRIVATE_PRIOR_WORK')
        before=self.book.path(private['id']).read_bytes();self.prepare();self.server.body=final()
        self.click('分析这道题');payload=self.server.requests[0]['payload']
        self.assertEqual(['search_notes'],[t['function']['name'] for t in payload['tools']])
        self.assertNotIn('PRIVATE_PRIOR_WORK',json.dumps(payload));self.assertNotIn(private['id'],json.dumps(payload))
        self.app.checkbox(key='study_agent_history').check().run()
        self.assertFalse(any(b.label=='放弃这次分析' for b in self.app.button))
        self.assertEqual(before,self.book.path(private['id']).read_bytes());self.assertEqual(1,len(self.server.requests))

    def test_saved_notebook_is_reported_honestly_if_decision_log_cannot_write(self):
        self.prepare();self.server.body=final();self.click('分析这道题')
        with patch('study.agent_ui.record_decision',side_effect=OSError('test log write failure')):
            self.click('确认加入错题本')
        self.assertEqual(1,len(self.book.list()[0]));self.assertIsNone(self.latest()['decision'])
        self.assertTrue(any('题目已保存，但资料查询记录未更新' in w.value for w in self.app.warning))
        self.assertEqual(1,len(self.server.requests))

    def test_new_question_does_not_display_previous_round_trace_even_for_same_text(self):
        self.prepare();self.server.body=final();self.click('分析这道题');previous=self.latest()['run_id']
        self.click('确认加入错题本');self.click('开始下一道题');self.prepare()
        self.assertFalse(any(e.label=='本次资料查询记录' for e in self.app.expander))
        self.assertIsNone(self.app.session_state.photo_analysis);self.assertEqual(1,len(self.server.requests))
        self.click('分析这道题');self.assertNotEqual(previous,self.latest()['run_id'])
        self.assertEqual(2,len(self.server.requests))

    def test_expired_candidate_does_not_write_and_failure_never_auto_retries(self):
        self.prepare();self.server.body=final();self.click('分析这道题')
        value=self.latest();value['finished_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.audit.save(value,'');self.click('确认加入错题本')
        self.assertFalse(self.book.directory.exists());self.assertEqual(1,len(self.server.requests))
        self.app.text_area(key='photo_work').set_value(STUDENT_WORK+'\n请核对').run()
        self.app.checkbox(key='photo_confirmed').check().run()
        self.server.body=calls(('bad','save_notebook',{}));self.click('分析这道题')
        self.click('分析这道题');self.app.run();self.assertEqual(2,len(self.server.requests))
        self.assertEqual('tool_not_allowed',self.latest()['error_code']);self.assertFalse(self.book.directory.exists())

    def test_correction_uses_loop_and_saves_only_after_confirmation(self):
        entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,analysis=ANALYSIS)
        self.book.save_new(entry);before=self.book.path(entry['id']).read_bytes()
        self.app.checkbox(key='study_agent_enabled').check().run()
        self.app.checkbox(key='study_agent_history').check().run()
        self.app.radio(key='study_view').set_value('错题本').run()
        answer,value=corrected_example()
        self.app.text_area(key='correction-answer-'+entry['id']).set_value(answer).run()
        self.app.radio(key='correction-kind-'+entry['id']).set_value('steps').run()
        self.app.checkbox(key='correction-confirm-'+entry['id']).check().run()
        responses=iter([calls(('h','get_review_history',{'topic':'方程','limit':3})),final(value)])
        self.server.body=lambda p:next(responses);self.click('分析本次订正')
        self.assertEqual(before,self.book.path(entry['id']).read_bytes())
        self.assertEqual('no_results',self.latest()['tool_calls'][0]['result']['status'])
        self.app.run();self.assertEqual(2,len(self.server.requests))
        self.click('确认保存本次订正');self.app.run()
        saved=self.book.get(entry['id']);self.assertEqual(1,len(saved['corrections']))
        self.assertEqual([],saved['reviews']);self.assertEqual('accept',self.latest()['decision']['action'])


if __name__=='__main__':unittest.main()
