"""照片、真实协议边界、错题持久化与页面流程；模型响应在测试中手写。"""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from streamlit.testing.v1 import AppTest
from http_test_support import LocalModelServer
from model_api import ModelAPIError, load_model_config
from study.images import prepare_image, image_bytes, MAX_UPLOAD
from study.notebook import Notebook, make_entry, summarize
from study.service import StudyService, PhotoTransport, validate_analysis, fingerprint
from study.example import ANALYSIS

ROOT=Path(__file__).parent


def chat_envelope(raw):
    return {'object':'chat.completion','model':'local-fixture','choices':[{'index':0,'finish_reason':'stop',
        'message':{'role':'assistant','content':raw}}],
        'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}

SOLUTION={'status':'solved','topic':'一元一次方程','summary':'用等式的性质把 x 单独留在左边。',
          'steps':['两边减去 3，得 2x = 8。','两边除以 2，得 x = 4。','代入检验：2 × 4 + 3 = 11。'],
          'answer':'x = 4','error_analysis':'未提供作答，不能确定具体错因。','next_practice':'为什么等式两边要同时减去 3？','clarification':''}


def new_solution(kind='none'):
    result=deepcopy(ANALYSIS)
    result['student_review']={'work_kind':kind,'verdict':'not_provided' if kind=='none' else 'uncertain',
        'observed_approach':'','answer_feedback':'' if kind=='none' else '请补充作答过程。','comparisons':[]}
    result['diagnosis']=[]
    return result


def sample():
    output=BytesIO()
    Image.new('RGB',(80,60),'white').save(output,'PNG')
    return output.getvalue()


class ImageTests(unittest.TestCase):
    def test_real_image_normalization_roundtrip(self):
        normalized=prepare_image(sample())
        raw=image_bytes(normalized)
        self.assertEqual('JPEG',Image.open(BytesIO(raw)).format)
        self.assertEqual((80,60),(normalized['width'],normalized['height']))

    def test_exif_is_removed(self):
        output=BytesIO()
        exif=Image.Exif();exif[270]='private metadata';exif[274]=6
        Image.new('RGB',(80,60),'white').save(output,'JPEG',exif=exif)
        clean=prepare_image(output.getvalue())
        restored=Image.open(BytesIO(image_bytes(clean)))
        self.assertEqual((60,80),restored.size)
        self.assertEqual({},dict(restored.getexif()))

    def test_fake_oversize_and_animated_files_rejected(self):
        for raw in (b'not image',b'',b'x'*(MAX_UPLOAD+1)):
            with self.assertRaises(ValueError): prepare_image(raw)
        output=BytesIO()
        Image.new('RGB',(10,10)).save(output,'GIF')
        with self.assertRaises(ValueError): prepare_image(output.getvalue())

    def test_tampered_image_rejected(self):
        image=prepare_image(sample());image['sha256']='bad'
        with self.assertRaises(ValueError): image_bytes(image)


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.folder=Path(self.temp)/'notebook'
        self.book=Notebook(self.folder)
        self.entry=make_entry(uuid4().hex,question='解方程：2x + 3 = 11',level='初中',image=prepare_image(sample()),analysis=SOLUTION)

    def test_draft_and_read_do_not_create_notebook(self):
        self.assertEqual(([],[]),self.book.list())
        self.assertFalse(self.folder.exists())

    def test_save_restart_duplicate_and_no_legacy_changes(self):
        legacy=Path(self.temp)/'fraction-add.json';legacy.write_text('old state')
        self.assertEqual('saved',self.book.save_new(self.entry))
        before=self.book.path(self.entry['id']).read_bytes()
        self.assertEqual('already_saved',self.book.save_new(self.entry))
        self.assertEqual(before,self.book.path(self.entry['id']).read_bytes())
        self.assertEqual(self.entry,Notebook(self.folder).get(self.entry['id']))
        self.assertEqual('old state',legacy.read_text())

    def test_same_id_different_content_is_not_overwritten(self):
        self.book.save_new(self.entry)
        other=deepcopy(self.entry);other['question']='different'
        with self.assertRaises(ValueError): self.book.save_new(other)
        self.assertEqual(self.entry,self.book.get(self.entry['id']))

    def test_invalid_record_does_not_write(self):
        for field,value in [('question',''),('level','大学'),('reason','模型认定你不会'),('id','../../outside')]:
            record=deepcopy(self.entry);record[field]=value
            with self.assertRaises(ValueError): self.book.save_new(record)
        self.assertFalse(self.folder.exists())

    def test_review_idempotence_stale_update_and_restart(self):
        self.book.save_new(self.entry)
        operation=uuid4().hex
        review={'outcome':'独立做对','answer':'x = 4','note':'先移项再化系数。'}
        updated=self.book.update(self.entry['id'],1,operation,review=review)
        self.assertEqual(2,updated['version'])
        self.assertEqual(updated,self.book.update(self.entry['id'],1,operation,review=review))
        with self.assertRaises(ValueError): self.book.update(self.entry['id'],1,uuid4().hex,review=review)
        self.assertEqual(1,len(Notebook(self.folder).get(self.entry['id'])['reviews']))

    def test_invalid_review_and_forbidden_edit_do_not_change_state(self):
        self.book.save_new(self.entry)
        for values in ({'review':{'outcome':'独立做对','answer':'','note':''}},
                       {'edit':{'question':'silently changed'}},
                       {'review':{'outcome':'已掌握','answer':'4','note':''}}):
            with self.assertRaises(ValueError): self.book.update(self.entry['id'],1,uuid4().hex,**values)
            self.assertEqual(self.entry,self.book.get(self.entry['id']))

    def test_bad_file_is_reported_and_preserved(self):
        self.folder.mkdir();damaged=self.folder/(uuid4().hex+'.json');damaged.write_text('broken')
        entries,errors=self.book.list()
        self.assertEqual([],entries);self.assertEqual(1,len(errors));self.assertEqual('broken',damaged.read_text())

    def test_failed_atomic_replace_keeps_original(self):
        self.book.save_new(self.entry)
        with patch('study.notebook.os.replace',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.book.update(self.entry['id'],1,uuid4().hex,edit={'topic':'方程','reason':'计算失误','correction':'检验'})
        self.assertEqual(self.entry,self.book.get(self.entry['id']))
        self.assertEqual([],list(self.folder.glob('.entry-*')))

    def test_summary_counts_latest_self_review_only(self):
        self.book.save_new(self.entry)
        self.assertEqual(1,summarize([self.entry])['needs_practice'])
        updated=self.book.update(self.entry['id'],1,uuid4().hex,review={'outcome':'独立做对','answer':'4','note':''})
        result=summarize([updated]);self.assertEqual(1,result['self_reported_correct']);self.assertEqual(0,result['needs_practice'])
        updated=self.book.update(self.entry['id'],2,uuid4().hex,review={'outcome':'仍需练习','answer':'','note':''})
        result=summarize([updated]);self.assertEqual(0,result['self_reported_correct']);self.assertEqual(2,result['review_count'])


class PhotoServiceTests(unittest.TestCase):
    def setUp(self):
        self.config=load_model_config(ROOT/'model_config.deepseek.example.json')

    def test_real_local_http_image_payload_and_strict_ocr(self):
        with LocalModelServer() as server:
            server.body=chat_envelope(json.dumps({'text':'解方程 2x + 3 = 11','student_work':'',
                'work_kind':'none','warnings':['请核对变量 x。']},ensure_ascii=False))
            tutor=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            result=tutor.recognize(prepare_image(sample()))
            self.assertIn('2x',result['text'])
            self.assertEqual(1,len(server.requests))
            blocks=server.requests[0]['payload']['messages'][1]['content']
            self.assertEqual('image_url',blocks[1]['type'])
            self.assertTrue(blocks[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))
            self.assertEqual('ok',tutor.calls[0]['status'])
            self.assertNotIn('local-test-key',json.dumps(tutor.calls))

    def test_general_question_analysis_preserves_user_context(self):
        with LocalModelServer() as server:
            server.body=chat_envelope(json.dumps(new_solution('answer_only'),ensure_ascii=False))
            tutor=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            result=tutor.analyze('解方程 2x + 3 = 11','初中','我算出 x = 7',work_kind='answer_only')
            self.assertEqual('x = 4',result['answer'])
            payload=server.requests[0]['payload']
            context=json.loads(payload['messages'][1]['content'][0]['text'])
            self.assertEqual('初中',context['school_level']);self.assertIn('x = 7',context['student_work'])
            self.assertEqual('answer_only',context['student_work_kind'])
            self.assertNotIn('fraction-add',json.dumps(payload))

    def test_missing_key_and_unconfirmed_invalid_question_never_send(self):
        with self.assertRaises(ModelAPIError): StudyService(self.config,'')
        with LocalModelServer() as server:
            tutor=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): tutor.analyze('','小学')
            self.assertEqual([],server.requests)

    def test_invalid_reply_and_network_error_are_not_retried(self):
        with LocalModelServer() as server:
            server.body=chat_envelope('{"extra":"bad"}')
            tutor=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): tutor.analyze('解方程','初中')
            self.assertEqual(1,len(server.requests))
            server.status=401
            with self.assertRaises(ModelAPIError): tutor.analyze('解方程','初中')
            self.assertEqual(2,len(server.requests))
            self.assertTrue(all(row['status']=='error' for row in tutor.calls))

    def test_credential_echo_rejected_and_not_logged(self):
        with LocalModelServer() as server:
            bad=deepcopy(SOLUTION);bad['summary']='local-test-key'
            server.body=chat_envelope(json.dumps(bad))
            tutor=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ModelAPIError): tutor.analyze('解方程','初中')
            self.assertNotIn('local-test-key',json.dumps(tutor.calls))

    def test_missing_conditions_cannot_claim_answer(self):
        data=new_solution();data.update(status='needs_clarification',clarification='请提供图形中的角度。')
        with self.assertRaises(ValueError): validate_analysis(data)
        data['answer']='';data['steps']=[]
        self.assertEqual(data,validate_analysis(data))
        data['extra']='state'
        with self.assertRaises(ValueError): validate_analysis(data)


def button(app,label): return next(b for b in app.button if b.label==label)


class PhotoAppTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.folder=Path(self.temp)
        self.enterContext(patch.dict(os.environ,{'MATH_ASSISTANT_DATA_DIR':self.temp,'MATH_ASSISTANT_START_VIEW':'拍照解题',
            'MATH_PHOTO_OFFLINE':'0','MATH_MODEL_CONFIG':str(ROOT/'model_config.example.json'),'DEEPSEEK_API_KEY':''}))
        self.app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=15).run()
        self.assertFalse(self.app.exception)

    def fill(self):
        self.app.text_area(key='photo_question').set_value('解方程：2x + 3 = 11').run()
        self.app.selectbox(key='photo_level').select('初中').run()
        self.app.checkbox(key='photo_confirmed').check().run()

    def test_no_confirmation_no_save_and_no_request(self):
        self.app.text_area(key='photo_question').set_value('解方程：2x + 3 = 11').run()
        self.assertTrue(button(self.app,'确认加入错题本').disabled)
        self.assertTrue(button(self.app,'分析这道题').disabled)
        self.assertFalse((self.folder/'notebook').exists())

    def test_manual_save_notebook_review_and_reload(self):
        self.fill()
        button(self.app,'确认加入错题本').click().run()
        self.assertFalse(self.app.exception)
        self.app.run()
        book=Notebook(self.folder/'notebook');entries,_=book.list();self.assertEqual(1,len(entries))
        self.app.radio(key='study_view').set_value('错题本').run()
        next(t for t in self.app.text_area if t.label=='本次答案或解题思路').set_value('x = 4')
        next(r for r in self.app.radio if r.label=='这次的情况').set_value('独立做对')
        button(self.app,'记录本次复习').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(1,len(book.get(entries[0]['id'])['reviews']))
        restored=AppTest.from_file(str(ROOT/'app.py'),default_timeout=15).run()
        restored.radio(key='study_view').set_value('学习回顾').run()
        self.assertFalse(restored.exception)
        self.assertEqual('1',next(m.value for m in restored.metric if m.label=='本次自评做对'))

    def test_question_change_requires_new_confirmation(self):
        self.fill()
        self.app.text_area(key='photo_question').set_value('解方程 x + 1 = 2').run()
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
        self.assertTrue(button(self.app,'确认加入错题本').disabled)

    def test_navigation_keeps_unsaved_question(self):
        self.fill()
        self.app.radio(key='study_view').set_value('错题本').run()
        self.app.radio(key='study_view').set_value('拍照解题').run()
        self.assertFalse(self.app.exception)
        self.assertEqual('解方程：2x + 3 = 11',self.app.text_area(key='photo_question').value)
        self.assertEqual('初中',self.app.selectbox(key='photo_level').value)
        self.assertFalse((self.folder/'notebook').exists())

    def test_failure_is_cached_rerun_does_not_send_and_can_clear(self):
        self.fill()
        with patch('study.ui.service',side_effect=ValueError('测试服务失败')) as mocked:
            button(self.app,'分析这道题').click().run()
            button(self.app,'分析这道题').click().run()
            self.app.run()
            self.assertEqual(1,mocked.call_count)
            self.assertFalse((self.folder/'notebook').exists())
            button(self.app,'清除失败记录，允许重新点击请求').click().run()
            self.assertEqual(1,mocked.call_count)

    def test_stale_analysis_is_not_saved_with_changed_question(self):
        self.fill()
        with patch('study.ui.service') as service:
            service.return_value.analyze.return_value=new_solution()
            button(self.app,'分析这道题').click().run()
            self.assertFalse(self.app.exception)
        self.app.text_area(key='photo_question').set_value('另一个问题 x + 1 = 3').run()
        self.app.checkbox(key='photo_confirmed').check().run()
        button(self.app,'确认加入错题本').click().run()
        self.assertFalse(self.app.exception)
        records,_=Notebook(self.folder/'notebook').list()
        self.assertIsNone(records[0]['analysis'])

    def test_key_clear_and_new_question_do_not_lose_saved_entries(self):
        self.app.text_input(key='photo_api_key').set_value('fake-not-real').run()
        button(self.app,'清除会话密钥').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual('',self.app.text_input(key='photo_api_key').value)
        self.fill();button(self.app,'确认加入错题本').click().run()
        button(self.app,'开始下一道题').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual('',self.app.text_area(key='photo_question').value)
        self.assertEqual(1,len(Notebook(self.folder/'notebook').list()[0]))


if __name__=='__main__': unittest.main(verbosity=2)
