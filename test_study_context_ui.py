"""用主页面实际按钮验收上下文隔离；仅发送本机手写 HTTP 响应。"""
import json
from copy import deepcopy
from uuid import uuid4
import unittest

from streamlit.testing.v1 import AppTest
import test_study as fixtures
import test_study_agent_ui as agent_fixtures
from test_study_agent import calls,final,history_entry
from test_study_context import COACH
from study.preferences import LocalPreferences
from study.example import QUESTION,STUDENT_WORK,ANALYSIS,corrected_example
from study.notebook import make_entry


class ContextAppTests(unittest.TestCase):
    setUp=agent_fixtures.AgentAppTests.setUp
    prepare=agent_fixtures.AgentAppTests.prepare
    click=agent_fixtures.AgentAppTests.click

    def value(self,index=-1):
        return json.loads(self.server.requests[index]['payload']['messages'][1]['content'][0]['text'])

    def request(self,text):
        next(w for w in self.app.text_area if w.label=='这次想弄懂什么（可选）').set_value(text).run()

    def task(self):return self.app.session_state.study_context_active

    def test_long_term_settings_require_save_and_restore_in_fresh_page(self):
        store=LocalPreferences(self.folder)
        control=self.app.selectbox(key='study-setting-presentation_density-0')
        control.select('detailed').run()
        self.assertFalse(store.path.exists());self.assertEqual('brief',store.load()['settings']['presentation_density'])
        self.click('确认保存长期设置');self.assertEqual('detailed',store.load()['settings']['presentation_density'])
        fresh=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        self.assertEqual('detailed',fresh.selectbox(key='study-setting-presentation_density-1').value)
        self.assertFalse(self.server.requests);self.assertFalse(self.book.directory.exists())

    def test_temporary_settings_consumed_after_analysis_and_next_round_uses_confirmed_values(self):
        self.prepare();self.server.body=final(ANALYSIS)
        next(w for w in self.app.selectbox if w.label=='这轮的信息密度').select('detailed').run()
        self.request('这次详细一点');self.click('分析这道题')
        self.assertEqual('detailed',self.value()['learning']['effective_settings']['presentation_density'])
        self.assertEqual('inherit',next(w for w in self.app.selectbox if w.label=='这轮的信息密度').value)
        self.assertEqual('',next(w for w in self.app.text_area if w.label=='这次想弄懂什么（可选）').value)
        self.click('分析这道题');self.assertEqual(1,len(self.server.requests))
        self.server.body=final(COACH);self.request('为什么要在两边同时减去？');self.click('只给一个提示')
        c=self.value();self.assertEqual('brief',c['learning']['effective_settings']['presentation_density'])
        self.assertEqual('hint',c['learning']['turn_request']['intent']);self.assertTrue(c['learning']['recent_dialogue'])
        self.assertEqual(STUDENT_WORK,c['student_work']);self.assertFalse(LocalPreferences(self.folder).path.exists())
        self.assertFalse(self.book.directory.exists())

    def test_same_question_followup_selection_and_new_question_are_isolated(self):
        self.prepare();self.server.body=final(COACH);self.click('只给一个提示')
        self.app.run();self.click('只给一个提示');self.assertEqual(1,len(self.server.requests))
        self.request('请再解释刚才这一步');self.click('继续讲解')
        self.assertEqual(COACH['reply'],self.value()['learning']['recent_dialogue'][-1]['model_reply'])
        self.click('把这一步作为接下来的任务');self.click('把这一步作为接下来的任务')
        self.assertEqual('next_step_selected',self.task()['status']);self.assertEqual([],self.task()['completed_steps'])
        self.assertEqual(2,len(self.server.requests));self.assertFalse(self.book.directory.exists())
        self.app.text_area(key='photo_question').set_value('计算 1/2 + 1/3。').run()
        self.assertEqual([],self.task()['turns']);self.assertEqual('',self.task()['selected_next_step'])
        self.assertTrue(fixtures.button(self.app,'继续讲解').disabled)

    def test_history_off_removes_prior_replies_and_does_not_prefetch_into_next_round(self):
        entry=history_entry(self.book,topic='方程',work='PRIVATE_HISTORY_WORK')
        self.prepare(history=True)
        reply={**COACH,'reply':'PRIVATE_HISTORY_REPLY：这只是以前一次的原作答。'}
        responses=iter([calls(('h','get_review_history',{'topic':'方程','limit':1})),final(reply)])
        self.server.body=lambda p:next(responses);self.click('继续讲解')
        self.assertTrue(self.task()['turns']);self.assertEqual(2,len(self.server.requests))
        self.app.checkbox(key='study_agent_history').uncheck().run()
        self.assertEqual([],self.task()['turns']);self.assertNotIn('coach',self.task())
        self.server.body=final(COACH);self.click('继续讲解')
        last=json.dumps(self.server.requests[-1]['payload'])
        self.assertNotIn('PRIVATE_HISTORY',last);self.assertNotIn(entry['id'],last)
        self.assertEqual(['search_notes'],[t['function']['name'] for t in self.server.requests[-1]['payload']['tools']])

    def test_archiving_or_disabling_source_invalidates_followup_cache(self):
        entry=history_entry(self.book,topic='方程');self.prepare(history=True)
        self.server.body=final(COACH);self.click('继续讲解');self.assertTrue(self.task()['turns'])
        LocalPreferences(self.folder,history=True).save(0,{entry['id']:{'enabled':False,'include_model':False}})
        self.app.run();self.assertEqual([],self.task()['turns']);self.assertNotIn('coach',self.task())
        self.click('继续讲解');self.assertEqual(2,len(self.server.requests))
        self.book.set_archived(entry['id'],entry['version'],uuid4().hex,archived=True)
        self.app.run();self.assertEqual([],self.task()['turns'])

    def test_invalid_coaching_result_stays_failed_across_rerenders(self):
        self.prepare();self.server.body=final({**COACH,'mastered':True})
        self.click('继续讲解');self.app.run();self.click('继续讲解')
        self.assertEqual(1,len(self.server.requests));self.assertEqual([],self.task()['turns'])
        self.assertFalse(self.book.directory.exists())

    def test_correction_comparison_uses_current_and_previous_work_without_saving(self):
        entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,analysis=ANALYSIS)
        self.book.save_new(entry);before=self.book.path(entry['id']).read_bytes()
        self.app.checkbox(key='study_agent_enabled').check().run()
        self.app.radio(key='study_view').set_value('错题本').run()
        answer,_=corrected_example()
        self.app.text_area(key='correction-answer-'+entry['id']).set_value(answer).run()
        self.app.radio(key='correction-kind-'+entry['id']).set_value('steps').run()
        self.assertTrue(fixtures.button(self.app,'比较本次订正').disabled)
        self.app.checkbox(key='correction-confirm-'+entry['id']).check().run()
        self.server.body=final(COACH);self.click('比较本次订正')
        c=self.value();self.assertEqual(STUDENT_WORK,c['previous_student_work']);self.assertEqual(answer,c['student_work'])
        self.assertEqual('compare',c['learning']['turn_request']['intent'])
        self.assertEqual(before,self.book.path(entry['id']).read_bytes())

    def test_per_record_controls_only_take_effect_after_confirmation(self):
        entry=history_entry(self.book,topic='方程');before=self.book.path(entry['id']).read_bytes()
        self.app.radio(key='study_view').set_value('错题本').run()
        next(w for w in self.app.checkbox if w.label=='允许引用这条历史的旧模型分析').uncheck().run()
        store=LocalPreferences(self.folder,history=True);self.assertFalse(store.path.exists())
        self.click('确认历史使用范围')
        self.assertFalse(store.load()['records'][entry['id']]['include_model'])
        self.assertEqual(before,self.book.path(entry['id']).read_bytes());self.assertFalse(self.server.requests)

    def test_default_single_request_mode_uses_same_context_without_agent_log(self):
        self.prepare();self.app.checkbox(key='study_agent_enabled').uncheck().run()
        self.server.body=fixtures.chat_envelope(json.dumps(ANALYSIS));self.request('请解释等式的性质');self.click('分析这道题')
        self.assertEqual('请解释等式的性质',self.value()['learning']['turn_request']['text'])
        self.server.body=fixtures.chat_envelope(json.dumps(COACH));self.click('只给一个提示')
        self.assertEqual(2,len(self.server.requests));self.assertEqual(1,len(self.value()['learning']['recent_dialogue']))
        self.assertFalse(self.audit.directory.exists());self.assertFalse(self.book.directory.exists())

    def test_rejected_analysis_is_not_used_in_followup(self):
        self.prepare();self.server.body=final(ANALYSIS);self.click('分析这道题')
        self.assertTrue(self.task()['turns']);self.click('放弃这次分析')
        self.assertEqual([],self.task()['turns'])
        self.server.body=final(COACH);self.click('继续讲解')
        self.assertEqual([],self.value()['learning']['recent_dialogue']);self.assertFalse(self.book.directory.exists())

    def test_return_to_current_unsaved_analysis_can_still_explain_its_steps(self):
        entry=history_entry(self.book,topic='方程');self.prepare();self.server.body=final(ANALYSIS);self.click('分析这道题')
        self.app.radio(key='study_view').set_value('错题本').run()
        self.app.radio(key='study_view').set_value('拍照解题').run()
        self.server.body=final(COACH);self.click('继续讲解')
        self.assertIn(ANALYSIS['steps'][0],self.value()['learning']['recent_dialogue'][0]['model_reply'])


if __name__=='__main__':unittest.main()
