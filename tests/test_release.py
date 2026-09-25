"""可公开交付的离线完整流程与发布文件边界。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest
from study.demo_service import DemoStudyService, ORIGIN, sample_image
from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
from study.notebook import Notebook, make_entry
from study.launch import launch_environment
from study.images import transform_image
from tools.export_public import export
from tests.test_study import ROOT, button


class DemoServiceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('study.service.PhotoTransport.send',side_effect=AssertionError('demo must not send HTTP')))
        self.service=DemoStudyService()

    def test_fixed_input_uses_validated_deep_copied_fixture(self):
        recognized=self.service.recognize(sample_image())
        self.assertEqual(STUDENT_WORK,recognized['student_work'])
        first=self.service.analyze(QUESTION,'初中',STUDENT_WORK,work_kind='steps')
        first['topic']='edited'
        self.assertEqual(ANALYSIS,self.service.analyze(QUESTION,'初中',STUDENT_WORK,work_kind='steps'))

    def test_arbitrary_inputs_crops_and_wrong_kind_are_rejected(self):
        with self.assertRaises(ValueError): self.service.recognize(transform_image(sample_image(),rotation=90))
        for q,work,kind in [('1+1',STUDENT_WORK,'steps'),(QUESTION,'x=99','steps'),(QUESTION,STUDENT_WORK,'answer_only')]:
            with self.assertRaises(ValueError): self.service.analyze(q,'初中',work,work_kind=kind)

    def test_correction_requires_original_analysis_and_never_changes_reviews(self):
        entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,analysis=ANALYSIS)
        before=deepcopy(entry);answer,result=corrected_example()
        self.assertEqual(result,self.service.reanalyze(entry,answer,work_kind='steps'))
        self.assertEqual(before,entry)
        entry['analysis']=None
        with self.assertRaises(ValueError): self.service.reanalyze(entry,answer,work_kind='steps')

    def test_demo_environment_removes_keys_and_isolates_notebook(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'synthetic','OPENAI_API_KEY':'synthetic','MATH_STUDY_DEMO':'1'}):
            env=launch_environment(ROOT,demo=True)
            self.assertNotIn('DEEPSEEK_API_KEY',env);self.assertNotIn('OPENAI_API_KEY',env)
            self.assertEqual(str(ROOT/'data/demo-study'),env['MATH_ASSISTANT_DATA_DIR'])
            self.assertEqual('1',env['MATH_PHOTO_OFFLINE'])
            self.assertEqual('0',launch_environment(ROOT)['MATH_STUDY_DEMO'])


class FullDemoAppTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.folder=Path(self.temp)
        self.enterContext(patch.dict(os.environ,{'MATH_ASSISTANT_DATA_DIR':self.temp,
            'MATH_ASSISTANT_START_VIEW':'拍照解题','MATH_MODEL_CONFIG':str(ROOT/'model_config.example.json'),
            'MATH_STUDY_DEMO':'1','MATH_PHOTO_OFFLINE':'1','DEEPSEEK_API_KEY':'must-not-be-used'}))
        self.enterContext(patch('study.service.PhotoTransport.send',side_effect=AssertionError('no real network')))
        self.app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=20).run()

    def test_replay_analyze_confirm_correct_reject_save_restore(self):
        button(self.app,'载入演示题').click().run()
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
        button(self.app,'识别这道题').click().run()
        self.assertTrue(any('人工预设转录' in w.value for w in self.app.warning))
        self.app.checkbox(key='photo_confirmed').check().run()
        button(self.app,'分析这道题').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(list(self.folder.rglob('*.json')))
        button(self.app,'确认加入错题本').click().run()
        book=Notebook(self.folder/'notebook');entry=book.list()[0][0]
        self.assertEqual(ORIGIN,entry['analysis_origin'])
        self.app.radio(key='study_view').set_value('错题本').run()
        button(self.app,'填入演示订正').click().run()
        self.app.checkbox(key='correction-confirm-'+entry['id']).check().run()
        button(self.app,'分析本次订正').click().run()
        before=book.path(entry['id']).read_bytes()
        button(self.app,'不保存本次分析').click().run()
        self.assertEqual(before,book.path(entry['id']).read_bytes())
        button(self.app,'分析本次订正').click().run()
        button(self.app,'确认保存本次订正').click().run()
        saved=book.get(entry['id']);self.assertEqual(1,len(saved['corrections']))
        self.assertEqual([],saved['reviews'])
        self.assertEqual(ORIGIN,saved['corrections'][0]['analysis_origin'])
        button(self.app,'移入回收站').click().run()
        self.app.radio(key='notebook_collection').set_value('回收站').run()
        button(self.app,'恢复这道题').click().run()
        restored=AppTest.from_file(str(ROOT/'app.py'),default_timeout=20).run()
        restored.radio(key='study_view').set_value('学习回顾').run()
        self.assertFalse(restored.exception)
        self.assertEqual(saved['corrections'],book.get(entry['id'])['corrections'])

    def test_demo_loader_does_not_overwrite_unsaved_work(self):
        self.app.text_area(key='photo_question').set_value('自己的题目').run()
        self.assertTrue(button(self.app,'载入演示题').disabled)
        self.assertEqual('自己的题目',self.app.text_area(key='photo_question').value)
        self.assertFalse(list(self.folder.rglob('*.json')))


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.root=Path(self.temp)/'source'
        self.root.mkdir();(self.root/'tools').mkdir();self.output=Path(self.temp)/'public'
        (self.root/'README.md').write_text('Self-authored public example')
        self.manifest(['README.md'])

    def manifest(self,files):
        (self.root/'tools/public_files.json').write_text(json.dumps({'files':files}))

    def test_exports_only_allowlist_and_refuses_existing_destination(self):
        (self.root/'.env').write_text('not published')
        (self.root/'data').mkdir();(self.root/'data/student.json').write_text('private')
        result=export(self.root,self.output)
        self.assertEqual({'README.md'},set(result['files']))
        self.assertEqual({'README.md','PUBLIC_MANIFEST.json'},{p.name for p in self.output.iterdir()})
        with self.assertRaises(ValueError): export(self.root,self.output)

    def test_blocked_paths_traversal_and_symlinks_fail_before_writing(self):
        for name in ('data/student.json','../secret.txt','.env.local','verification/private.json','/tmp/file'):
            self.manifest([name])
            with self.assertRaises(ValueError): export(self.root,self.output)
            self.assertFalse(self.output.exists())
        (self.root/'link.py').symlink_to(self.root/'README.md');self.manifest(['link.py'])
        with self.assertRaises(ValueError): export(self.root,self.output)

    def test_suspected_credentials_and_private_paths_are_rejected_without_echo(self):
        for value in ('ghp_'+'A'*40,'/'.join(['','Users','example','Documents','Codex','private']), '/'.join(['','Users','example','Desktop','private']), '/'.join(['','home','example','private']), chr(92).join(['C:', 'Users', 'example', 'private'])):
            (self.root/'README.md').write_text(value)
            with self.assertRaises(ValueError) as error: export(self.root,self.output)
            self.assertNotIn(value,str(error.exception));self.assertFalse(self.output.exists())


if __name__=='__main__': unittest.main(verbosity=2)
