"""v2 作答对照的证据、保存边界及页面；所有模型回复均为手写测试数据。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest
from http_test_support import LocalModelServer
from model_api import load_model_config
from study.diagnosis import validate_analysis
from study.example import QUESTION, STUDENT_WORK, ANALYSIS
from study.images import prepare_image
from study.notebook import Notebook, make_entry, learning_groups, summarize
from study.service import StudyService, PhotoTransport, fingerprint
import test_study as fixtures


class DiagnosisTests(unittest.TestCase):
    def test_wrong_steps_have_traceable_comparison_and_focus(self):
        result=validate_analysis(deepcopy(ANALYSIS),student_work=STUDENT_WORK,work_kind='steps')
        self.assertEqual('等式的基本性质',result['diagnosis'][0]['knowledge_point'])
        self.assertIn(result['diagnosis'][0]['evidence'],STUDENT_WORK)

    def test_invented_student_step_rejected(self):
        result=deepcopy(ANALYSIS)
        result['student_review']['comparisons'][0]['student_excerpt']='2x = 11 - 3'
        with self.assertRaisesRegex(ValueError,'没有提供'): validate_analysis(result,student_work=STUDENT_WORK)

    def test_answer_only_cannot_invent_a_method_or_diagnosis(self):
        answer=fixtures.new_solution('answer_only')
        answer['student_review'].update(verdict='incorrect',answer_feedback='x = 7 代入后不满足原方程。')
        validate_analysis(answer,student_work='x = 7',work_kind='answer_only')
        for field,value in [('observed_approach','你移项没有变号'),
                            ('comparisons',ANALYSIS['student_review']['comparisons'])]:
            bad=deepcopy(answer);bad['student_review'][field]=value
            with self.assertRaises(ValueError): validate_analysis(bad,student_work='x = 7')
        bad=deepcopy(answer);bad['diagnosis']=deepcopy(ANALYSIS['diagnosis'])
        with self.assertRaises(ValueError): validate_analysis(bad,student_work='x = 7')

    def test_kind_cannot_be_changed_and_no_work_cannot_be_graded(self):
        with self.assertRaises(ValueError):
            validate_analysis(ANALYSIS,student_work=STUDENT_WORK,work_kind='answer_only')
        bad=fixtures.new_solution();bad['student_review']['verdict']='incorrect'
        with self.assertRaises(ValueError): validate_analysis(bad,student_work='')
        with self.assertRaises(ValueError): validate_analysis(fixtures.new_solution(),student_work='x = 7')

    def test_diagnosis_needs_wrong_step_evidence_and_known_category(self):
        for field,value in [('evidence','x = 4'),('category','不聪明'),('knowledge_point','完全不会数学')]:
            bad=deepcopy(ANALYSIS);bad['diagnosis'][0][field]=value
            with self.assertRaises(ValueError): validate_analysis(bad,student_work=STUDENT_WORK)
        bad=deepcopy(ANALYSIS)
        # 原文虽存在，但已标为正确的步骤不能成为错因证据。
        bad['student_review']['comparisons'][0]['verdict']='correct'
        with self.assertRaises(ValueError): validate_analysis(bad,student_work=STUDENT_WORK)

    def test_answer_only_rejects_empty_diagnosis_inside_student_review(self):
        # 手写最小复现：合法 JSON 仍不允许未知嵌套字段；不会自动删字段修复。
        result=fixtures.new_solution('answer_only')
        for move_from_top in (False, True):
            bad=deepcopy(result)
            bad['student_review']['diagnosis']=bad.pop('diagnosis') if move_from_top else []
            original=deepcopy(bad)
            with self.subTest(move_from_top=move_from_top):
                with self.assertRaises(ValueError):
                    validate_analysis(bad,student_work='x = 4',work_kind='answer_only')
                self.assertEqual(original,bad)
        validate_analysis(result,student_work='x = 4',work_kind='answer_only')

    def test_correct_reduction_after_wrong_addition_is_not_error_evidence(self):
        # 手写测试数据验证“整体错误、局部正确”可表达；不证明模型会如此判断。
        result=json.loads((fixtures.ROOT/'evaluation/study_smoke_local_responses.json').read_text())['responses']['b02']
        work='2/3 + 1/6 = 3/9\n3/9 = 1/3'
        validate_analysis(result,student_work=work,work_kind='steps')
        self.assertEqual('incorrect',result['student_review']['verdict'])
        self.assertEqual(['incorrect','correct'],[row['verdict'] for row in result['student_review']['comparisons']])
        bad=deepcopy(result)
        bad['diagnosis'][0]['evidence']='3/9 = 1/3'
        with self.assertRaisesRegex(ValueError,'错因未对应'):
            validate_analysis(bad,student_work=work,work_kind='steps')

    def test_equivalent_correct_method_can_be_accepted_without_error_label(self):
        result=deepcopy(ANALYSIS)
        result['student_review'].update(verdict='correct',observed_approach='用逆运算一次列出表达式。',
            answer_feedback='结果代入成立。',comparisons=[{'student_excerpt':'x = (11 - 3) / 2 = 4',
                'reference_step':'先两边减 3，再除以 2。','verdict':'correct','explanation':'合并写法与逐步逆运算等价。'}])
        result['diagnosis']=[]
        validate_analysis(result,student_work='x = (11 - 3) / 2 = 4',work_kind='steps')

    def test_missing_conditions_must_not_solve_or_diagnose(self):
        bad=deepcopy(ANALYSIS);bad.update(status='needs_clarification',clarification='图中长度是多少？',answer='')
        with self.assertRaises(ValueError): validate_analysis(bad,student_work=STUDENT_WORK)
        result=fixtures.new_solution('unclear')
        result.update(status='needs_clarification',clarification='请补充图形长度。',answer='',steps=[],knowledge_points=[],takeaway='')
        validate_analysis(result,student_work='看不清')

    def test_malformed_nested_data_and_extra_state_are_rejected(self):
        cases=[]
        for field in ('work_kind','verdict','comparisons'):
            bad=deepcopy(ANALYSIS);bad['student_review'][field]={'unexpected':True};cases.append(bad)
        bad=deepcopy(ANALYSIS);bad['completed']=True;cases.append(bad)
        bad=deepcopy(ANALYSIS);bad['schema_version']=True;cases.append(bad)
        bad=deepcopy(ANALYSIS);bad['knowledge_points']=['重复','重复'];cases.append(bad)
        for bad in cases:
            with self.subTest(value=bad):
                with self.assertRaises(ValueError): validate_analysis(bad,student_work=STUDENT_WORK)

    def test_input_kind_and_work_change_analysis_fingerprint(self):
        one=fingerprint(QUESTION,'初中','x=7',None,'answer_only')
        self.assertNotEqual(one,fingerprint(QUESTION,'初中','x=7',None,'steps'))
        self.assertNotEqual(one,fingerprint(QUESTION,'初中','x=4',None,'answer_only'))

    def test_legacy_saved_analysis_reads_without_rewrite_but_new_api_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            entry=make_entry(uuid4().hex,question=QUESTION,level='初中',analysis=fixtures.SOLUTION)
            book=Notebook(directory);book.save_new(entry)
            before=book.path(entry['id']).read_bytes()
            self.assertEqual(entry,Notebook(directory).get(entry['id']))
            self.assertEqual(before,book.path(entry['id']).read_bytes())
            with self.assertRaises(ValueError): validate_analysis(fixtures.SOLUTION)

    def test_diagnosis_saved_only_on_confirmation_and_restored_for_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)/'book';book=Notebook(folder)
            entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,
                             topic='一元一次方程',analysis=ANALYSIS,correction='先写出两边同时减 3。')
            self.assertFalse(folder.exists())
            book.save_new(entry)
            self.assertEqual('already_saved',book.save_new(entry))
            entries,errors=Notebook(folder).list()
            self.assertFalse(errors)
            self.assertEqual(ANALYSIS,entries[0]['analysis'])
            self.assertEqual({'尚不确定':1},summarize(entries)['reasons'])
            group=learning_groups(entries)[0]
            self.assertEqual(1,group['needs_practice'])
            self.assertEqual(ANALYSIS['takeaway'],group['notes'][0]['takeaway'])
            self.assertEqual('先写出两边同时减 3。',group['notes'][0]['correction'])

    def test_tampered_diagnosis_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Notebook(Path(directory)/'book')
            entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,analysis=ANALYSIS)
            entry['my_work']='x = 4'
            with self.assertRaises(ValueError): book.save_new(entry)
            self.assertFalse(book.directory.exists())


class DiagnosticHTTPTests(unittest.TestCase):
    def test_photo_separates_original_wrong_work_then_sends_confirmed_work(self):
        with LocalModelServer() as server:
            tutor=StudyService(load_model_config(fixtures.ROOT/'model_config.deepseek.example.json'),
                               'local-test-key',PhotoTransport(server.chat_url))
            image=prepare_image(fixtures.sample())
            server.body=fixtures.chat_envelope(json.dumps({'text':QUESTION,'student_work':STUDENT_WORK,
                                                          'work_kind':'steps','warnings':[]},ensure_ascii=False))
            recognized=tutor.recognize(image)
            self.assertEqual(STUDENT_WORK,recognized['student_work'])
            server.body=fixtures.chat_envelope(json.dumps(ANALYSIS,ensure_ascii=False))
            result=tutor.analyze(recognized['text'],'初中',recognized['student_work'],image,work_kind='steps')
            self.assertEqual(ANALYSIS,result)
            context=json.loads(server.requests[1]['payload']['messages'][1]['content'][0]['text'])
            self.assertEqual(STUDENT_WORK,context['student_work'])
            self.assertEqual('steps',context['student_work_kind'])
            self.assertEqual(2,len(server.requests))
            self.assertEqual('photo-study-v4',tutor.calls[1]['contract'])

    def test_bad_evidence_or_wrong_kind_fails_once_without_retry(self):
        with LocalModelServer() as server:
            tutor=StudyService(load_model_config(fixtures.ROOT/'model_config.deepseek.example.json'),
                               'local-test-key',PhotoTransport(server.chat_url))
            server.body=fixtures.chat_envelope(json.dumps(ANALYSIS))
            with self.assertRaises(ValueError): tutor.analyze(QUESTION,'初中','x = 7',work_kind='answer_only')
            self.assertEqual(1,len(server.requests))
            self.assertEqual('invalid_content',tutor.calls[0]['error_code'])
            with self.assertRaises(ValueError): tutor.analyze(QUESTION,'初中','',work_kind='steps')
            self.assertEqual(1,len(server.requests))
            server.body=fixtures.chat_envelope(json.dumps({'text':QUESTION,'student_work':'', 'work_kind':'steps','warnings':[]}))
            with self.assertRaises(ValueError): tutor.recognize(prepare_image(fixtures.sample()))
            self.assertEqual(2,len(server.requests))


class DiagnosticAppTests(unittest.TestCase):
    setUp=fixtures.PhotoAppTests.setUp
    fill=fixtures.PhotoAppTests.fill

    def fill_steps(self):
        self.fill()
        self.app.text_area(key='photo_work').set_value(STUDENT_WORK).run()
        self.app.radio(key='photo_work_kind').set_value('steps').run()
        self.app.checkbox(key='photo_confirmed').check().run()

    def test_ocr_separates_question_and_student_answer_for_user_confirmation(self):
        self.app.session_state['photo_image']=prepare_image(fixtures.sample())
        self.app.run()
        with patch('study.ui.service') as mocked:
            mocked.return_value.recognize.return_value={'text':QUESTION,'student_work':STUDENT_WORK,'work_kind':'steps','warnings':[]}
            fixtures.button(self.app,'识别这道题').click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(QUESTION,self.app.text_area(key='photo_question').value)
            self.assertEqual(STUDENT_WORK,self.app.text_area(key='photo_work').value)
            self.assertEqual('steps',self.app.radio(key='photo_work_kind').value)
            self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
            self.assertTrue(fixtures.button(self.app,'分析这道题').disabled)
            self.assertFalse((self.folder/'notebook').exists())
            mocked.return_value.analyze.assert_not_called()

    def test_comparison_save_reopen_and_grouped_review(self):
        self.fill_steps()
        with patch('study.ui.service') as mocked:
            mocked.return_value.analyze.return_value=deepcopy(ANALYSIS)
            fixtures.button(self.app,'分析这道题').click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual('steps',mocked.return_value.analyze.call_args.kwargs['work_kind'])
            self.assertFalse((self.folder/'notebook').exists())
        self.assertEqual('一元一次方程',next(t.value for t in self.app.text_input if t.label=='知识点 / 分类'))
        self.assertTrue(any('同类题怎么做' in m.value for m in self.app.markdown))
        fixtures.button(self.app,'确认加入错题本').click().run()
        entries,_=Notebook(self.folder/'notebook').list()
        self.assertEqual(ANALYSIS,entries[0]['analysis'])
        self.assertEqual('尚不确定',entries[0]['reason'])
        fresh=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        fresh.radio(key='study_view').set_value('错题本').run()
        self.assertFalse(fresh.exception)
        fresh.radio(key='study_view').set_value('学习回顾').run()
        self.assertFalse(fresh.exception)
        self.assertTrue(any(ANALYSIS['takeaway'] in m.value for m in fresh.markdown))
        self.assertTrue(any('尚不确定 · 1 题' in m.value for m in fresh.markdown))

    def test_changed_work_invalidates_confirmation_and_old_analysis(self):
        self.fill_steps()
        with patch('study.ui.service') as mocked:
            mocked.return_value.analyze.return_value=deepcopy(ANALYSIS)
            fixtures.button(self.app,'分析这道题').click().run()
        self.app.text_area(key='photo_work').set_value('x = 4').run()
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
        self.app.radio(key='photo_work_kind').set_value('answer_only').run()
        self.app.checkbox(key='photo_confirmed').check().run()
        fixtures.button(self.app,'确认加入错题本').click().run()
        entries,_=Notebook(self.folder/'notebook').list()
        self.assertIsNone(entries[0]['analysis'])

    def test_work_kind_survives_navigation_and_change_invalidates_confirmation(self):
        self.fill_steps()
        self.app.radio(key='study_view').set_value('学习回顾').run()
        self.app.radio(key='study_view').set_value('拍照解题').run()
        self.assertEqual('steps',self.app.radio(key='photo_work_kind').value)
        self.assertEqual(STUDENT_WORK,self.app.text_area(key='photo_work').value)
        self.app.radio(key='photo_work_kind').set_value('answer_only').run()
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)

    def test_offline_example_has_no_model_calls_or_saved_record(self):
        with patch('study.ui.service',side_effect=AssertionError('示例禁止请求')):
            self.app.run()
            self.assertFalse(self.app.exception)
            self.assertTrue(any('这是人工编写的示例' in info.value for info in self.app.info))
            self.assertFalse((self.folder/'notebook').exists())


if __name__=='__main__': unittest.main(verbosity=2)
