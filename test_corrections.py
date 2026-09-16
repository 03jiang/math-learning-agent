"""订正闭环使用手写模型响应，验证请求、前后证据、确认保存及重启。"""
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
from study.corrections import baseline, fingerprint, validate_result
from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
from study.notebook import Notebook, make_entry, summarize, learning_groups
from study.service import StudyService, PhotoTransport
import test_study as fixtures


def entry():
    return make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,
                      topic='一元一次方程',analysis=ANALYSIS,analysis_origin='手写测试数据')


class CorrectionProtocolTests(unittest.TestCase):
    def check(self,result,answer=None,previous=None):
        return validate_result(result,previous_work=STUDENT_WORK,previous_analysis=previous or ANALYSIS,
                               answer=answer or corrected_example()[0],work_kind=result['analysis']['student_review']['work_kind'])

    def test_corrected_steps_have_two_sided_evidence(self):
        answer,result=corrected_example()
        self.assertEqual(result,self.check(result))

    def test_fabricated_previous_or_current_excerpt_rejected(self):
        for key in ('previous_excerpt','current_excerpt'):
            _,result=corrected_example();result['comparison']['changes'][0][key]='x = 100'
            with self.assertRaises(ValueError): self.check(result)

    def test_corrected_claim_must_match_both_analyses(self):
        _,result=corrected_example()
        result['comparison']['changes'][0]['current_excerpt']='2x = 11 - 3'
        result['analysis']['student_review']['comparisons'][0]['verdict']='uncertain'
        result['analysis']['student_review']['verdict']='uncertain'
        with self.assertRaises(ValueError): self.check(result)
        _,result=corrected_example();result['comparison']['changes'][0]['status']='still_incorrect'
        with self.assertRaises(ValueError): self.check(result)

    def test_answer_only_cannot_claim_a_step_was_corrected(self):
        _,result=corrected_example()
        result['analysis']=fixtures.new_solution('answer_only')
        result['analysis']['student_review'].update(verdict='correct',answer_feedback='x = 4 代入成立。')
        with self.assertRaises(ValueError): self.check(result,answer='x = 4')
        result['comparison']['changes']=[]
        result['comparison']['summary']='本次答案正确，但没有过程，无法确认原步骤是否已订正。'
        self.check(result,answer='x = 4')

    def test_missing_previous_analysis_cannot_claim_specific_progress(self):
        answer,result=corrected_example()
        with self.assertRaises(ValueError):
            validate_result(result,previous_work=STUDENT_WORK,previous_analysis=None,answer=answer,work_kind='steps')
        result['comparison']['changes']=[]
        validate_result(result,previous_work=STUDENT_WORK,previous_analysis=None,answer=answer,work_kind='steps')

    def test_missing_conditions_and_extra_mastery_field_rejected(self):
        _,result=corrected_example();result['mastered']=True
        with self.assertRaises(ValueError): self.check(result)
        _,result=corrected_example();result['comparison']['changes'][0]['status']='掌握'
        with self.assertRaises(ValueError): self.check(result)
        _,result=corrected_example()
        result['analysis']=fixtures.new_solution('unclear')
        result['analysis'].update(status='needs_clarification',answer='',steps=[],clarification='请补充图形条件。')
        with self.assertRaises(ValueError): self.check(result,answer='看不清')


class CorrectionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.book=Notebook(Path(self.temp)/'book');self.entry=entry();self.book.save_new(self.entry)
        self.before=self.book.path(self.entry['id']).read_bytes()
        self.answer,self.result=corrected_example()
        self.operation=uuid4().hex

    def save(self,version=1,based_on='original',operation=None,result=None):
        return self.book.add_correction(self.entry['id'],version,operation or self.operation,based_on=based_on,
            answer=self.answer,work_kind='steps',result=result or self.result,analysis_origin='手写测试数据')

    def test_preview_and_legacy_read_never_write(self):
        original=self.book.get(self.entry['id'])
        baseline(original);fingerprint(original,self.answer,'steps')
        validate_result(self.result,previous_work=STUDENT_WORK,previous_analysis=ANALYSIS,answer=self.answer,work_kind='steps')
        self.assertEqual(self.before,self.book.path(self.entry['id']).read_bytes())
        self.assertEqual(1,original['schema_version'])

    def test_confirm_restores_v2_keeps_original_and_does_not_mark_mastery(self):
        saved=self.save()
        self.assertEqual(2,saved['schema_version'])
        for key in ('my_work','analysis','reviews','reason','correction'):
            self.assertEqual(self.entry[key],saved[key])
        self.assertEqual(self.result,saved['corrections'][0]['result'])
        self.assertEqual(saved,Notebook(self.book.directory).get(saved['id']))
        self.assertEqual(0,summarize([saved])['self_reported_correct'])
        note=learning_groups([saved])[0]['notes'][0]
        self.assertEqual(1,note['correction_count'])
        self.assertEqual(self.result['analysis']['next_practice'],note['next_practice'])

    def test_duplicate_confirm_is_idempotent_and_changed_payload_rejected(self):
        saved=self.save();before=self.book.path(saved['id']).read_bytes()
        self.assertEqual(saved,self.save())
        bad=deepcopy(self.result);bad['comparison']['summary']='different'
        with self.assertRaises(ValueError): self.save(result=bad)
        self.assertEqual(before,self.book.path(saved['id']).read_bytes())

    def test_stale_version_and_wrong_baseline_do_not_write(self):
        saved=self.save();before=self.book.path(saved['id']).read_bytes()
        with self.assertRaises(ValueError): self.save(operation=uuid4().hex)
        with self.assertRaises(ValueError): self.save(version=2,operation=uuid4().hex,based_on='original')
        self.assertEqual(before,self.book.path(saved['id']).read_bytes())

    def test_second_correction_compares_to_last_saved_and_preserves_chain(self):
        first=self.save()
        result=deepcopy(self.result);result['comparison']={'summary':'本次继续使用正确步骤。','changes':[]}
        second=self.save(version=2,based_on=self.operation,operation=uuid4().hex,result=result)
        self.assertEqual(2,len(second['corrections']))
        self.assertEqual(first['corrections'][0],second['corrections'][0])
        self.assertEqual(second['corrections'][-1]['id'],baseline(second)['id'])
        self.assertEqual(self.answer,baseline(second)['work'])
        self.assertEqual(second,Notebook(self.book.directory).get(second['id']))

    def test_invalid_evidence_or_failed_write_keeps_v1_bytes(self):
        bad=deepcopy(self.result);bad['comparison']['changes'][0]['current_excerpt']='x = 100'
        with self.assertRaises(ValueError): self.save(result=bad)
        self.assertEqual(self.before,self.book.path(self.entry['id']).read_bytes())
        with patch('study.notebook.os.replace',side_effect=OSError('disk failed')):
            with self.assertRaises(OSError): self.save()
        self.assertEqual(self.before,self.book.path(self.entry['id']).read_bytes())
        self.assertFalse(list(self.book.directory.glob('.entry-*')))

    def test_corrupted_chain_is_reported_not_rewritten(self):
        saved=self.save();saved['corrections'][0]['based_on']=uuid4().hex
        path=self.book.path(saved['id']);path.write_text(json.dumps(saved));before=path.read_bytes()
        records,errors=self.book.list()
        self.assertEqual([],records);self.assertEqual(1,len(errors));self.assertEqual(before,path.read_bytes())


class CorrectionHTTPTests(unittest.TestCase):
    def test_one_request_contains_only_this_question_and_previous_attempt(self):
        item=entry();answer,result=corrected_example()
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps(result,ensure_ascii=False))
            tutor=StudyService(load_model_config(fixtures.ROOT/'model_config.deepseek.example.json'),
                               'local-test-key',PhotoTransport(server.chat_url))
            self.assertEqual(result,tutor.reanalyze(item,answer,work_kind='steps'))
            self.assertEqual(1,len(server.requests))
            payload=server.requests[0]['payload'];context=json.loads(payload['messages'][1]['content'][0]['text'])
            self.assertEqual(STUDENT_WORK,context['previous_student_work'])
            self.assertEqual(answer,context['student_work'])
            self.assertEqual(ANALYSIS,context['previous_analysis'])
            self.assertNotIn('reviews',context);self.assertNotIn('operation_ids',context)
            self.assertEqual('photo-correction-v5',tutor.calls[0]['contract'])

    def test_invalid_comparison_has_no_retry_and_empty_work_never_sends(self):
        answer,result=corrected_example();result['comparison']['changes'][0]['previous_excerpt']='unseen work'
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps(result))
            tutor=StudyService(load_model_config(fixtures.ROOT/'model_config.deepseek.example.json'),
                               'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): tutor.reanalyze(entry(),answer,work_kind='steps')
            self.assertEqual(1,len(server.requests))
            with self.assertRaises(ValueError): tutor.reanalyze(entry(),' ',work_kind='steps')
            self.assertEqual(1,len(server.requests))


class CorrectionAppTests(unittest.TestCase):
    setUp=fixtures.PhotoAppTests.setUp

    def open_entry(self):
        self.entry=entry();self.book=Notebook(self.folder/'notebook');self.book.save_new(self.entry)
        self.path=self.book.path(self.entry['id']);self.before=self.path.read_bytes()
        self.app.radio(key='study_view').set_value('错题本').run()
        self.answer,self.result=corrected_example()
        self.answer_key='correction-answer-'+self.entry['id']
        self.confirm_key='correction-confirm-'+self.entry['id']
        self.app.text_area(key=self.answer_key).set_value(self.answer).run()
        self.app.radio(key='correction-kind-'+self.entry['id']).set_value('steps').run()

    def analyze(self):
        self.app.checkbox(key=self.confirm_key).check().run()
        with patch('study.ui.service') as mocked:
            mocked.return_value.reanalyze.return_value=self.result
            fixtures.button(self.app,'分析本次订正').click().run()
            self.assertFalse(self.app.exception)
            return mocked

    def test_unconfirmed_no_request_preview_rejection_no_write(self):
        self.open_entry()
        self.assertTrue(fixtures.button(self.app,'分析本次订正').disabled)
        self.analyze()
        self.assertEqual(self.before,self.path.read_bytes())
        fixtures.button(self.app,'不保存本次分析').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(any(b.label=='确认保存本次订正' for b in self.app.button))
        self.assertEqual(self.before,self.path.read_bytes())

    def test_confirmation_persists_history_and_restarts_without_marking_correct(self):
        self.open_entry();self.analyze()
        fixtures.button(self.app,'确认保存本次订正').click().run()
        self.assertFalse(self.app.exception)
        self.app.run()
        saved=self.book.get(self.entry['id'])
        self.assertEqual(1,len(saved['corrections']));self.assertEqual([],saved['reviews'])
        fresh=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        fresh.radio(key='study_view').set_value('错题本').run()
        self.assertFalse(fresh.exception)
        self.assertTrue(any('第 1 次订正' in e.label for e in fresh.expander))
        fresh.radio(key='study_view').set_value('学习回顾').run()
        self.assertFalse(fresh.exception)
        self.assertEqual('0',next(m.value for m in fresh.metric if m.label=='本次自评做对'))
        self.assertTrue(any(self.result['comparison']['summary'] in m.value for m in fresh.markdown))

    def test_editing_work_invalidates_preview_and_keeps_draft_on_navigation(self):
        self.open_entry();self.analyze()
        self.app.text_area(key=self.answer_key).set_value('x = 4').run()
        self.assertFalse(self.app.checkbox(key=self.confirm_key).value)
        self.assertFalse(any(b.label=='确认保存本次订正' for b in self.app.button))
        self.app.radio(key='study_view').set_value('学习回顾').run()
        self.app.radio(key='study_view').set_value('错题本').run()
        self.assertEqual('x = 4',self.app.text_area(key=self.answer_key).value)
        self.assertEqual(self.before,self.path.read_bytes())

    def test_other_update_makes_old_preview_unsavable(self):
        self.open_entry();self.analyze()
        self.book.update(self.entry['id'],1,uuid4().hex,edit={'topic':'方程','reason':'尚不确定','correction':'新的笔记'})
        self.app.run()
        self.assertFalse(any(b.label=='确认保存本次订正' for b in self.app.button))
        self.assertNotIn('corrections',self.book.get(self.entry['id']))

    def test_failed_request_is_cached_until_explicit_clear(self):
        self.open_entry();self.app.checkbox(key=self.confirm_key).check().run()
        with patch('study.ui.service',side_effect=ValueError('测试服务失败')) as mocked:
            fixtures.button(self.app,'分析本次订正').click().run()
            fixtures.button(self.app,'分析本次订正').click().run()
            self.assertEqual(1,mocked.call_count)
            fixtures.button(self.app,'清除此订正的失败记录').click().run()
            self.assertEqual(1,mocked.call_count)
        self.assertEqual(self.before,self.path.read_bytes())


if __name__=='__main__': unittest.main(verbosity=2)
