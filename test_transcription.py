"""原始识图、人工核对、确认保存和重启恢复；不访问真实模型。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest
from study.images import prepare_image, transform_image
from study.notebook import Notebook, make_entry, validate_entry
from study.transcription import observe, confirm, validate_recognition
from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
import test_study as fixtures


def raw_reply():
    return {'text':QUESTION+'（识别多出文字）','student_work':STUDENT_WORK,'work_kind':'steps','warnings':['请核对题目末尾。']}


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))/'notebook'
        self.book=Notebook(self.directory)
        self.image=prepare_image(fixtures.sample())
        self.raw=raw_reply()
        self.observation=observe(self.raw,self.image,mode='local_http_test',origin='本机手写响应')
        self.trace=confirm(self.observation,QUESTION,STUDENT_WORK,'steps',self.image)
        self.entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,
            image=self.image,analysis=ANALYSIS,transcription=self.trace)

    def test_draft_has_no_write_raw_is_independent_and_edits_are_explicit(self):
        self.assertFalse(self.directory.exists())
        self.assertEqual(['text'],self.trace['changed_fields'])
        self.raw['text']='调用者后来改动'
        self.assertEqual(raw_reply(),self.entry['transcription']['observation']['raw'])
        self.assertEqual(QUESTION,self.entry['question'])

    def test_save_duplicate_and_fresh_process_restore_both_versions(self):
        self.book.save_new(self.entry)
        before=self.book.path(self.entry['id']).read_bytes()
        self.assertEqual('already_saved',self.book.save_new(self.entry))
        self.assertEqual(before,self.book.path(self.entry['id']).read_bytes())
        script='from study.notebook import Notebook; import sys,json; print(json.dumps(Notebook(sys.argv[1]).get(sys.argv[2])))'
        result=subprocess.check_output([sys.executable,'-B','-c',script,str(self.directory),self.entry['id']],cwd=fixtures.ROOT,text=True)
        self.assertEqual(self.entry,json.loads(result))
        self.assertEqual([],json.loads(result)['reviews'])

    def test_changed_image_tampered_raw_or_mismatched_confirmed_content_cannot_save(self):
        bad_values=[]
        for modify in (
            lambda e:e['transcription']['observation']['raw'].update(text='改写原始识别'),
            lambda e:e.update(question='偷偷改题目'),
            lambda e:e['transcription'].update(changed_fields=[]),
            lambda e:e.update(image=transform_image(self.image,horizontal=(0,50))),
            lambda e:e['transcription']['confirmed'].update(work_kind='answer_only')):
            bad=deepcopy(self.entry);modify(bad);bad_values.append(bad)
        for bad in bad_values:
            with self.subTest(bad=bad['question']):
                with self.assertRaises(ValueError): self.book.save_new(bad)
        self.assertFalse(self.directory.exists())

    def test_archive_restore_and_correction_keep_trace_and_schema(self):
        self.book.save_new(self.entry)
        self.book.set_archived(self.entry['id'],1,uuid4().hex,archived=True)
        self.book.set_archived(self.entry['id'],2,uuid4().hex,archived=False)
        answer,result=corrected_example()
        saved=self.book.add_correction(self.entry['id'],3,uuid4().hex,based_on='original',answer=answer,
            work_kind='steps',result=result,analysis_origin='本机手写响应')
        self.assertEqual(4,saved['schema_version'])
        self.assertEqual(self.trace,saved['transcription'])
        self.assertEqual(saved,Notebook(self.directory).get(saved['id']))

    def test_work_photo_only_keeps_original_typed_question(self):
        raw=raw_reply();raw['text']=QUESTION
        original=observe(raw,None,self.image,provided_question=QUESTION,mode='local_http_test',origin='本机手写响应')
        self.assertEqual(QUESTION,original['provided_question'])
        confirm(original,QUESTION,STUDENT_WORK,'steps',None,self.image)
        with self.assertRaises(ValueError):
            observe(raw,None,self.image,provided_question='另一道题',mode='local_http_test',origin='本机手写响应')

    def test_invalid_ocr_structure_and_legacy_records(self):
        for key,value in [('work_kind','none'),('student_work',''),('warnings','not a list'),('text','')]:
            raw=raw_reply();raw[key]=value
            with self.assertRaises(ValueError): validate_recognition(raw)
        for schema in (1,2,3):
            old=deepcopy(self.entry);old.pop('transcription');old['schema_version']=schema
            if schema<3:
                for name in ('work_image','archived_at','archive_events'): old.pop(name)
            if schema<2: old.pop('corrections')
            old['id']=uuid4().hex
            self.book.save_new(old);before=self.book.path(old['id']).read_bytes()
            self.assertEqual(old,self.book.get(old['id']))
            self.assertEqual(before,self.book.path(old['id']).read_bytes())


class TranscriptionAppTests(unittest.TestCase):
    setUp=fixtures.PhotoAppTests.setUp

    def recognize(self):
        self.app.session_state['photo_image']=prepare_image(fixtures.sample())
        self.app.run()
        with patch('study.ui.service') as service:
            service.return_value.recognize.return_value=raw_reply()
            fixtures.button(self.app,'识别这道题').click().run()
        self.assertFalse(self.app.exception)

    def test_ocr_edits_analysis_save_and_restart(self):
        self.recognize()
        self.assertFalse((self.folder/'notebook').exists())
        self.assertTrue(fixtures.button(self.app,'确认加入错题本').disabled)
        original=deepcopy(self.app.session_state['photo_observation'])
        self.app.text_area(key='photo_question').set_value(QUESTION).run()
        self.app.selectbox(key='photo_level').set_value('初中').run()
        self.app.checkbox(key='photo_confirmed').check().run()
        with patch('study.ui.service') as service:
            service.return_value.analyze.return_value=ANALYSIS
            fixtures.button(self.app,'分析这道题').click().run()
            self.assertEqual(QUESTION,service.return_value.analyze.call_args.args[0])
            self.assertEqual(STUDENT_WORK,service.return_value.analyze.call_args.args[2])
        self.assertFalse((self.folder/'notebook').exists())
        fixtures.button(self.app,'确认加入错题本').click().run()
        self.assertFalse(self.app.exception)
        entry=Notebook(self.folder/'notebook').list()[0][0]
        self.assertEqual(original,entry['transcription']['observation'])
        self.assertEqual(['text'],entry['transcription']['changed_fields'])
        restored=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        restored.radio(key='study_view').set_value('错题本').run()
        self.assertFalse(restored.exception)
        self.assertTrue(any(e.label=='对照原始识别与核对内容' for e in restored.expander))
        self.assertTrue(any(raw_reply()['text'] in t.value for t in restored.text))

    def test_ocr_cache_keeps_original_timestamp_and_new_photo_discards_trace(self):
        self.recognize();original=deepcopy(self.app.session_state['photo_observation'])
        with patch('study.ui.service',side_effect=AssertionError('缓存不得发请求')):
            fixtures.button(self.app,'识别这道题').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(original,self.app.session_state['photo_observation'])
        fixtures.button(self.app,'移除题目照片').click().run()
        self.assertIsNone(self.app.session_state['photo_observation'])
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)


if __name__=='__main__': unittest.main(verbosity=2)
