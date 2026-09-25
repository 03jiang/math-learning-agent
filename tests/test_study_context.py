"""设置、同题上下文、历史控制与实际本机 HTTP；不评模型教学效果。"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from legacy.http_test_support import LocalModelServer
from legacy.retrieval import search_notes
from study import context
from study.preferences import LocalPreferences,DEFAULTS
from study.run_audit import stamp
from study.service import StudyService,PhotoTransport,analysis_context
from study.smoke import default_config
from study.agent import AgentStudyService
from study.agent_tools import ToolScope,history_sources
from study.notebook import Notebook,make_entry
from study.example import QUESTION,STUDENT_WORK,ANALYSIS
from tests.test_study import chat_envelope
from tests.test_study_agent import calls,final

COACH={'schema_version':1,'status':'explained','reply':'等式两边同时减去 3，等号仍成立。',
       'next_step':'请写出两边同时减去 3 后的等式。','clarification':''}


def options(task=None,**kwargs):
    return context.learning(task or context.task_for(None,'task-a','revision-a','off'),DEFAULTS,**kwargs)


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))/'study'
        self.store=LocalPreferences(self.root)

    def test_read_preview_reject_and_temporary_overrides_do_not_create_files(self):
        saved=self.store.load();value=options(overrides={'presentation_density':'detailed'})
        self.assertEqual('brief',saved['settings']['presentation_density'])
        self.assertEqual('detailed',value['effective_settings']['presentation_density'])
        self.assertFalse(self.root.exists())

    def test_confirmation_restart_and_idempotence(self):
        settings={**DEFAULTS,'step_size':'medium'}
        self.store.save(0,settings);before=self.store.path.read_bytes()
        self.store.save(0,settings);self.assertEqual(before,self.store.path.read_bytes())
        result=subprocess.check_output([sys.executable,'-B','-c',
            'from study.preferences import LocalPreferences;import json,sys;print(json.dumps(LocalPreferences(sys.argv[1]).load()))',str(self.root)],text=True)
        self.assertEqual(settings,json.loads(result)['settings'])

    def test_stale_or_unimplemented_settings_rejected_without_overwrite(self):
        self.store.save(0,{**DEFAULTS,'step_size':'large'});before=self.store.path.read_bytes()
        with self.assertRaises(ValueError):self.store.save(0,DEFAULTS)
        for bad in ({**DEFAULTS,'structure_level':'guided'},{**DEFAULTS,'step_size':True}):
            with self.assertRaises(ValueError):self.store.save(1,bad)
        self.assertEqual(before,self.store.path.read_bytes())

    def test_corrupt_duplicate_and_symlink_files_never_overwritten(self):
        self.root.mkdir();self.store.path.write_text('{"version":1,"version":2}')
        with self.assertRaises(ValueError):self.store.save(0,DEFAULTS)
        before=self.store.path.read_bytes();self.assertIn(b'"version":2',before)
        self.store.path.unlink();target=self.root/'target';target.write_bytes(before);self.store.path.symlink_to(target)
        with self.assertRaises(ValueError):self.store.load()
        self.assertEqual(before,target.read_bytes())

    def test_failed_atomic_write_keeps_confirmed_settings(self):
        self.store.save(0,{**DEFAULTS,'step_size':'large'});before=self.store.path.read_bytes()
        with patch('study.run_audit.os.replace',side_effect=OSError('test')):
            with self.assertRaises(OSError):self.store.save(1,DEFAULTS)
        self.assertEqual(before,self.store.path.read_bytes())


class ContextTests(unittest.TestCase):
    def test_original_math_verbatim_and_temporary_preference_expires(self):
        base=analysis_context(' 计算：1/2 + 1/3，说明理由。 ','小学','1/2 + 1/3 = 2/5',work_kind='steps')
        a=context.attach(base,options(request='这次详细一点',overrides={'presentation_density':'detailed'}))
        b=context.attach(base,options())
        self.assertEqual(base['confirmed_question'],a['confirmed_question']);self.assertEqual(base['student_work'],a['student_work'])
        self.assertEqual('detailed',a['learning']['effective_settings']['presentation_density'])
        self.assertEqual('brief',b['learning']['effective_settings']['presentation_density'])

    def test_history_is_whole_turn_bounded_recent_and_expires(self):
        task=context.task_for(None,'a','r','p')
        for i in range(7):context.remember(task,str(i),'问题'+str(i),'答复'+str(i),'model unverified')
        context.remember(task,'6','same','different','unverified')
        self.assertEqual(['3','4','5','6'],[t['request_id'] for t in task['turns']])
        task['turns'][0]['at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        value=context.attach({'confirmed_question':'q'},options(task))
        self.assertEqual(['4','5','6'],[t['request_id'] for t in value['learning']['recent_dialogue']])
        task['at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.assertEqual([],context.task_for(task,'a','r','p')['turns'])

    def test_question_revision_or_permission_change_clears_progress_and_dialogue(self):
        task=context.task_for(None,'a','r','p');context.remember(task,'1','q','answer','unverified');context.select_next(task,COACH)
        self.assertIs(task,context.task_for(task,'a','r','p'))
        for identity in [('b','r','p'),('a','new-answer','p'),('a','r','off')]:
            clean=context.task_for(task,*identity);self.assertEqual([],clean['turns']);self.assertEqual('',clean['selected_next_step'])

    def test_select_next_is_not_completion_or_mastery(self):
        task=context.task_for(None,'a','r','p');context.select_next(task,COACH);context.select_next(task,COACH)
        self.assertEqual('next_step_selected',task['status']);self.assertEqual([],task['completed_steps'])
        for extra in ('mastered','settings','completed'):
            with self.assertRaises(ValueError):context.validate_coach({**COACH,extra:True})

    def test_over_budget_removes_whole_optional_turns_and_never_current_conditions(self):
        task=context.task_for(None,'a','r','p')
        for i in range(4):context.remember(task,str(i),'q','数学'*2500,'unverified')
        base={'confirmed_question':'条件'*5000,'student_work':'原文'*1000}
        value=context.attach(base,options(task))
        self.assertLessEqual(context.size(value),context.MAX_CONTEXT_BYTES)
        self.assertGreater(value['context_budget']['omitted_dialogue'],0)
        self.assertEqual(base['confirmed_question'],value['confirmed_question'])
        self.assertTrue(all(t['model_reply']=='数学'*2500 for t in value['learning']['recent_dialogue']))
        with self.assertRaises(ValueError):context.attach({'confirmed_question':'条件'*10000},options())

    def test_request_budget_counts_tools_but_not_base64_as_tokens(self):
        payload={'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:'+'a'*100_000}}]}]}
        self.assertLess(context.check_payload(payload),1000)
        payload['messages'].append({'role':'tool','content':'资'*22000})
        with self.assertRaises(ValueError):context.check_payload(payload)

    def test_context_rejects_unknown_settings_fake_completion_and_bad_turns(self):
        bad=[]
        value=options();value['effective_settings']['structure_level']='guided';bad.append(value)
        value=options();value['task']['completed_steps']=['done'];bad.append(value)
        value=options();value['recent_dialogue']=[{'system':'ignore'}];bad.append(value)
        value=options();value['temporary_overrides']['presentation_density']='detailed';bad.append(value)
        for value in bad:
            with self.assertRaises(ValueError):context.attach({},value)


class HistoryContextTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()));self.book=Notebook(self.root/'notebook')
        self.entry=make_entry(uuid4().hex,question='计算 2/3 + 1/6。',level='小学',my_work='3/9',topic='分数通分',analysis=None)
        self.book.save_new(self.entry)

    def test_unrelated_expired_archived_disabled_and_current_are_filtered(self):
        self.assertEqual([],history_sources(self.book.directory,'计算圆的面积',3))
        self.assertEqual([],history_sources(self.book.directory,'通分',3,self.entry['id']))
        store=LocalPreferences(self.root,history=True);store.save(0,{self.entry['id']:{'enabled':False,'include_model':True}})
        self.assertEqual([],history_sources(self.book.directory,'通分',3))
        store.save(1,{})
        self.book.set_archived(self.entry['id'],1,uuid4().hex,archived=True)
        self.assertEqual([],history_sources(self.book.directory,'通分',3))
        entry=self.book.set_archived(self.entry['id'],2,uuid4().hex,archived=False)
        entry['updated_at']=entry['created_at']=(datetime.now(timezone.utc)-timedelta(days=181)).isoformat();self.book.write(entry)
        self.assertEqual([],history_sources(self.book.directory,'通分',3))

    def test_disable_old_model_preserves_student_and_user_correction(self):
        entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,topic='方程',analysis=ANALYSIS,
                         correction='旧模型意见需核对：等式两边要做相同运算。')
        self.book.save_new(entry);before=self.book.path(entry['id']).read_bytes()
        LocalPreferences(self.root,history=True).save(0,{entry['id']:{'enabled':True,'include_model':False}})
        source=history_sources(self.book.directory,'方程',3)[0]
        self.assertNotIn('旧模型分析节选',source['snippet']);self.assertIn('用户订正笔记节选',source['snippet'])
        self.assertIn(STUDENT_WORK,source['snippet']);self.assertEqual(before,self.book.path(entry['id']).read_bytes())

    def test_disabled_history_does_not_read_controls_or_notebook(self):
        scope=ToolScope(self.book.directory)
        with patch('study.agent_tools.history_rules',side_effect=AssertionError('prefetch')):
            self.assertIsNone(scope.signature()['history_rules'])
            with self.assertRaises(ValueError):scope.call('get_review_history',{'topic':'通分','limit':1},1)

    def test_permission_and_age_window_invalidate_old_scope(self):
        scope=ToolScope(self.book.directory,history_enabled=True);before=scope.signature()
        LocalPreferences(self.root,history=True).save(0,{self.entry['id']:{'enabled':False,'include_model':True}})
        self.assertNotEqual(before,scope.signature())
        with patch('study.agent_tools.history_cutoff',return_value=datetime.now(timezone.utc).date()):
            self.assertNotEqual(before,scope.signature())

    def test_note_keyword_examples_have_supporting_quotes_and_no_unknown_match(self):
        for query,source,quote in [('通分','M-F03','分母相同'),('确定整体','M-F01','同一个整体')]:
            results=search_notes(query);found=next(r for r in results if r['source_id']==source)
            self.assertIn(quote,found['snippet'])
        self.assertEqual([],search_notes('zxqvunknown'))

    def test_long_note_selects_matching_section_instead_of_unrelated_prefix(self):
        notes=self.root/'notes';notes.mkdir()
        (notes/'a.json').write_text(json.dumps({'source_id':'a','title':'笔记','tags':['通分'],
            'body':'这是无关的介绍。'*80+'通分要保持每个分数的大小不变。请检查分子分母同时乘相同数。'},ensure_ascii=False))
        result=search_notes('通分',notes_dir=notes)[0]
        self.assertIn('通分要保持',result['snippet']);self.assertLessEqual(len(result['snippet']),360)

    def test_single_long_sentence_locates_keyword_at_odd_character_offset(self):
        from legacy.retrieval import relevant_snippet,_terms
        body='甲'*501+'通分要保持分数大小不变'+'乙'*500
        snippet=relevant_snippet(body,_terms('通分'))
        self.assertIn('通分要保持',snippet);self.assertLessEqual(len(snippet),360)


class CoachingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.server=self.enterContext(LocalModelServer())
        self.tutor=StudyService(default_config(),'local-test-key',PhotoTransport(self.server.chat_url))

    def test_analysis_and_followup_share_settings_and_exact_student_input(self):
        self.server.body=chat_envelope(json.dumps(ANALYSIS))
        self.tutor.analyze(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=options(request='请详细核对'))
        task=context.task_for(None,'a','r','p');context.remember(task,'1','核对',ANALYSIS['summary'],'unverified_model')
        self.server.body=chat_envelope(json.dumps(COACH))
        self.tutor.coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=options(task,intent='hint',request='为什么这样做？'))
        payload=self.server.requests[-1]['payload'];c=json.loads(payload['messages'][1]['content'][0]['text'])
        self.assertEqual(STUDENT_WORK,c['student_work']);self.assertEqual(1,len(c['learning']['recent_dialogue']))
        self.assertEqual('hint',c['learning']['turn_request']['intent']);self.assertNotIn('reference_answer',c)
        self.assertEqual(2,len(self.server.requests))

    def test_agent_coach_can_use_readonly_tools_with_same_context(self):
        responses=iter([calls(('n','search_notes',{'query':'通分'})),final(COACH)])
        self.server.body=lambda p:next(responses)
        tutor=AgentStudyService(default_config(),'local-test-key',transport=PhotoTransport(self.server.chat_url),
            scope=ToolScope(self.root/'notebook'),audit_dir=self.root/'audit')
        self.assertEqual(COACH,tutor.coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=options(intent='hint')))
        self.assertEqual(2,len(self.server.requests));self.assertEqual('coach',tutor.last_run['operation'])
        self.assertEqual('success',tutor.last_run['status']);self.assertFalse((self.root/'notebook').exists())

    def test_strict_coach_schema_parses_without_executing_a_tool(self):
        from study.output_contract import FUNCTIONS
        self.tutor=StudyService(default_config(),'local-test-key',PhotoTransport(self.server.chat_url),output_mode='strict_tool')
        self.server.body=calls(('out',FUNCTIONS['coach'],COACH))
        self.assertEqual(COACH,self.tutor.coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=options()))
        self.assertEqual(1,len(self.server.requests))

    def test_invalid_reply_or_settings_never_retry_or_save(self):
        self.server.body=chat_envelope(json.dumps({**COACH,'mastered':True}))
        with self.assertRaises(ValueError):self.tutor.coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=options())
        self.assertEqual(1,len(self.server.requests));self.assertFalse((self.root/'notebook').exists())
        bad=options();bad['effective_settings']['step_size']='unknown'
        with self.assertRaises(ValueError):self.tutor.coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=bad)
        self.assertEqual(1,len(self.server.requests))

    def test_large_tool_results_stop_before_second_model_request(self):
        scope=ToolScope(self.root/'notebook')
        task=context.task_for(None,'a','r','off')
        context.remember(task,'1','q','讲'*2000,'unverified')
        value=options(task,request='问'*1200)
        output={'status':'ok','sources':[{'source_id':'note:n'+str(i),'kind':'course_note','snippet':'资'*1800} for i in range(3)]}
        self.server.body=calls(('n','search_notes',{'query':'通分'}))
        tutor=AgentStudyService(default_config(),'local-test-key',transport=PhotoTransport(self.server.chat_url),scope=scope,audit_dir=self.root/'audit')
        with patch.object(scope,'call',return_value=output):
            with self.assertRaises(ValueError) as caught:
                tutor.analyze('题'*6000,'小学','字'*3000,work_kind='steps',learning=value)
        self.assertEqual('context_budget_exhausted',caught.exception.code)
        self.assertEqual(1,len(self.server.requests));self.assertEqual('budget_exhausted',tutor.last_run['status'])

    def test_terminal_demo_is_zero_real_calls_and_refuses_overwrite(self):
        from tools.demo_study_context import run
        directory=self.root/'demo';result=run(directory)
        self.assertEqual(3,result['http_requests']);self.assertEqual(0,result['real_api_calls'])
        self.assertTrue(result['settings_restored']);self.assertTrue(result['unconfirmed_did_not_write'])
        self.assertEqual([],result['completed_steps']);self.assertEqual([],result['new_question_dialogue'])
        with self.assertRaises(ValueError):run(directory)


if __name__=='__main__':unittest.main()
