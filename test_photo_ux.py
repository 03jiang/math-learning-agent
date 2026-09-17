"""双图、裁剪、旋转与可恢复删除；只用合成图片和本机 HTTP 手写响应。"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from streamlit.testing.v1 import AppTest
from http_test_support import LocalModelServer
from model_api import load_model_config, ModelAPIError
from study.images import prepare_image, image_bytes, transform_image
from study.notebook import Notebook, make_entry, summarize, validate_entry
from study.photo_ui import put_upload, remove_photo
from study.service import StudyService, PhotoTransport, fingerprint
from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
import test_study as fixtures


def colored():
    image=Image.new('RGB',(80,60),'white')
    for box,color in [((0,0,40,30),'red'),((40,0,80,30),'green'),((0,30,40,60),'blue'),((40,30,80,60),'yellow')]:
        image.paste(color,box)
    output=BytesIO();image.save(output,'PNG');return output.getvalue()


class ImageEditTests(unittest.TestCase):
    def test_clockwise_rotation_then_crop_retains_expected_region(self):
        original=prepare_image(colored())
        cropped=transform_image(original,rotation=90,horizontal=(0,50),vertical=(0,50))
        self.assertEqual((30,40),(cropped['width'],cropped['height']))
        with Image.open(BytesIO(image_bytes(cropped))) as image:
            r,g,b=image.getpixel((15,20))
            self.assertGreater(b,200);self.assertLess(r,60);self.assertLess(g,60)
            self.assertFalse(dict(image.getexif()))

    def test_noop_and_repeat_from_original_do_not_accumulate_edits(self):
        original=prepare_image(colored());before=deepcopy(original)
        self.assertEqual(original,transform_image(original))
        one=transform_image(original,horizontal=(0,50))
        self.assertEqual(one,transform_image(original,horizontal=(0,50)))
        self.assertEqual(before,original)

    def test_invalid_crop_rotation_and_tampered_image_rejected(self):
        original=prepare_image(colored())
        for args in ({'rotation':True},{'rotation':45},{'horizontal':(50,50)}, {'vertical':(-1,50)},
                     {'vertical':(0,101)},{'horizontal':(0,'50')},{'horizontal':(0,0.5)}):
            with self.subTest(args=args):
                with self.assertRaises(ValueError): transform_image(original,**args)
        original['sha256']='wrong'
        with self.assertRaises(ValueError): transform_image(original)

    def test_work_photo_changes_invalidate_request_fingerprint(self):
        original=prepare_image(colored())
        cropped=transform_image(original,horizontal=(0,50))
        first=fingerprint(QUESTION,'初中',STUDENT_WORK,original,'steps',work_image=original)
        self.assertNotEqual(first,fingerprint(QUESTION,'初中',STUDENT_WORK,original,'steps',work_image=cropped))


class State(dict):
    def __getattr__(self,key):
        try: return self[key]
        except KeyError as exc: raise AttributeError(key) from exc
    def __setattr__(self,key,value): self[key]=value


class PhotoDraftTests(unittest.TestCase):
    def setUp(self):
        self.state=State(photo_image=prepare_image(fixtures.sample()),photo_work_image=prepare_image(colored()),
            photo_question=QUESTION,photo_work=STUDENT_WORK,photo_confirmed=True,photo_analysis={'old':'analysis'},
            photo_recognition={'old':'text'})
        self.enterContext(patch('study.photo_ui.st.session_state',self.state))

    def test_replacing_question_drops_unrelated_work_photo_and_confirmation(self):
        put_upload('question',colored())
        self.assertIsNone(self.state.photo_work_image)
        self.assertEqual('',self.state.photo_question);self.assertEqual('',self.state.photo_work)
        self.assertIsNone(self.state.photo_analysis);self.assertFalse(self.state.photo_confirmed)

    def test_replacing_work_photo_keeps_question_but_clears_old_work(self):
        before=deepcopy(self.state.photo_image)
        put_upload('work',fixtures.sample())
        self.assertEqual(QUESTION,self.state.photo_question);self.assertEqual(before,self.state.photo_image)
        self.assertEqual('',self.state.photo_work);self.assertFalse(self.state.photo_confirmed)

    def test_legacy_upload_hash_preserves_unsaved_text_on_code_reload(self):
        self.state.photo_uploaded_hash=sha256(fixtures.sample()).hexdigest()
        put_upload('question',fixtures.sample())
        self.assertEqual(QUESTION,self.state.photo_question);self.assertEqual(STUDENT_WORK,self.state.photo_work)
        self.assertTrue(self.state.photo_confirmed)

    def test_invalid_upload_and_remove_never_replace_other_image(self):
        before=deepcopy(self.state)
        with self.assertRaises(ValueError): put_upload('work',b'bad image')
        self.assertEqual(before,self.state)
        remove_photo('work')
        self.assertIsNone(self.state.photo_work_image)
        self.assertEqual(before['photo_image'],self.state.photo_image)
        self.assertFalse(self.state.photo_confirmed)


class TwoPhotoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.config=load_model_config(fixtures.ROOT/'model_config.deepseek.example.json')
        self.question=prepare_image(fixtures.sample());self.work=prepare_image(colored())

    def test_two_photos_have_correct_roles_and_send_applied_crop(self):
        crop=transform_image(self.work,horizontal=(0,50))
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps({'text':QUESTION,'student_work':STUDENT_WORK,'work_kind':'steps','warnings':[]}))
            service=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            service.recognize(self.question,crop)
            blocks=server.requests[0]['payload']['messages'][1]['content']
            self.assertEqual(3,len(blocks))
            roles=json.loads(blocks[0]['text'])['attached_images']
            self.assertEqual([{'position':1,'role':'question'},{'position':2,'role':'student_work'}],roles)
            self.assertEqual('data:image/jpeg;base64,'+crop['base64'],blocks[2]['image_url']['url'])
            server.body=fixtures.chat_envelope(json.dumps(ANALYSIS))
            service.analyze(QUESTION,'初中',STUDENT_WORK,self.question,work_kind='steps',work_image=crop)
            self.assertEqual(2,len(server.requests))
            self.assertEqual('photo-study-v6',service.calls[-1]['contract'])

    def test_work_only_requires_typed_question_and_never_fabricates_question(self):
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps({'text':QUESTION,'student_work':'x = 4','work_kind':'answer_only','warnings':[]}))
            service=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): service.recognize(None,self.work)
            self.assertEqual([],server.requests)
            service.recognize(None,self.work,question_text=QUESTION)
            blocks=server.requests[0]['payload']['messages'][1]['content']
            context=json.loads(blocks[0]['text'])
            self.assertEqual(QUESTION,context['provided_question'])
            self.assertEqual([{'position':1,'role':'student_work'}],context['attached_images'])

    def test_total_payload_limit_fails_before_http(self):
        record={'attempted_requests':0}
        with LocalModelServer() as server:
            with self.assertRaises(ModelAPIError):
                PhotoTransport(server.chat_url).send({'text':'x'*(12*1024*1024)},'local-test-key',2,record)
            self.assertEqual([],server.requests)

    def test_work_only_response_cannot_rewrite_typed_question(self):
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps({'text':'模型另造的题目','student_work':'x = 4',
                                                          'work_kind':'answer_only','warnings':[]}))
            service=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): service.recognize(None,self.work,question_text=QUESTION)
            self.assertEqual(1,len(server.requests))
            self.assertEqual('error',service.calls[-1]['status'])

    def test_reanalysis_uses_both_original_photos_but_current_work_text(self):
        answer,result=corrected_example()
        item=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,
            image=self.question,work_image=self.work,analysis=ANALYSIS)
        with LocalModelServer() as server:
            server.body=fixtures.chat_envelope(json.dumps(result))
            service=StudyService(self.config,'local-test-key',PhotoTransport(server.chat_url))
            service.reanalyze(item,answer,work_kind='steps')
            blocks=server.requests[0]['payload']['messages'][1]['content']
            self.assertEqual(3,len(blocks));self.assertEqual(answer,json.loads(blocks[0]['text'])['student_work'])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory());self.book=Notebook(Path(self.temp)/'book')
        self.entry=make_entry(uuid4().hex,question=QUESTION,level='初中',my_work=STUDENT_WORK,
            image=prepare_image(fixtures.sample()),work_image=prepare_image(colored()),analysis=ANALYSIS)
        self.book.save_new(self.entry);self.path=self.book.path(self.entry['id'])

    def archive(self,version=1,operation=None,archived=True):
        return self.book.set_archived(self.entry['id'],version,operation or uuid4().hex,archived=archived)

    def test_two_photos_restore_from_disk_and_old_schema_reads_without_rewrite(self):
        self.assertEqual(3,self.entry['schema_version'])
        self.assertEqual(self.entry,Notebook(self.book.directory).get(self.entry['id']))
        old=make_entry(uuid4().hex,question='1+1',level='小学')
        self.book.save_new(old)
        path=self.book.path(old['id']);before=path.read_bytes()
        self.assertEqual(old,self.book.get(old['id']))
        self.assertEqual(before,path.read_bytes())
        archived=self.book.set_archived(old['id'],1,uuid4().hex,archived=True)
        self.assertEqual(3,archived['schema_version'])
        self.assertEqual(old['question'],archived['question'])

    def test_archive_excluded_from_summary_and_restore_keeps_content(self):
        archived=self.archive()
        self.assertEqual([],self.book.list()[0])
        self.assertEqual(0,summarize(self.book.list()[0])['total'])
        self.assertEqual([archived],Notebook(self.book.directory).list(archived=True)[0])
        restored=self.archive(version=2,archived=False)
        self.assertEqual(3,restored['version']);self.assertIsNone(restored['archived_at'])
        self.assertEqual([restored],self.book.list()[0])
        for key in ('my_work','analysis','image','work_image','reviews','corrections'):
            self.assertEqual(self.entry[key],restored[key])

    def test_duplicate_delete_and_restore_are_idempotent(self):
        operation=uuid4().hex;archived=self.archive(operation=operation);before=self.path.read_bytes()
        self.assertEqual(archived,self.archive(operation=operation));self.assertEqual(before,self.path.read_bytes())
        restored=self.archive(version=2,archived=False)
        self.assertEqual(restored,self.archive(operation=operation))
        self.assertIsNone(self.book.get(self.entry['id'])['archived_at'])

    def test_stale_operations_do_not_overwrite(self):
        self.archive();before=self.path.read_bytes()
        with self.assertRaises(ValueError): self.archive(version=1,archived=False)
        self.assertEqual(before,self.path.read_bytes())

    def test_restore_does_not_reactivate_pending_correction_from_before_delete(self):
        self.archive();self.archive(version=2,archived=False)
        before=self.path.read_bytes();answer,result=corrected_example()
        with self.assertRaises(ValueError):
            self.book.add_correction(self.entry['id'],1,uuid4().hex,based_on='original',answer=answer,
                work_kind='steps',result=result,analysis_origin='过期测试数据')
        self.assertEqual(before,self.path.read_bytes())

    def test_archived_entry_blocks_edits_corrections_and_model_requests(self):
        archived=self.archive();before=self.path.read_bytes();answer,result=corrected_example()
        with self.assertRaises(ValueError):
            self.book.update(archived['id'],2,uuid4().hex,edit={'topic':'方程','reason':'尚不确定','correction':'不应保存'})
        with self.assertRaises(ValueError):
            self.book.add_correction(archived['id'],2,uuid4().hex,based_on='original',answer=answer,
                work_kind='steps',result=result,analysis_origin='测试')
        with LocalModelServer() as server:
            service=StudyService(load_model_config(fixtures.ROOT/'model_config.deepseek.example.json'),
                                 'local-test-key',PhotoTransport(server.chat_url))
            with self.assertRaises(ValueError): service.reanalyze(archived,answer,work_kind='steps')
            self.assertEqual([],server.requests)
        self.assertEqual(before,self.path.read_bytes())

    def test_correction_history_survives_v3_archive_and_restore(self):
        answer,result=corrected_example()
        corrected=self.book.add_correction(self.entry['id'],1,uuid4().hex,based_on='original',answer=answer,
            work_kind='steps',result=result,analysis_origin='手写测试数据')
        self.assertEqual(3,corrected['schema_version'])
        self.archive(version=2);restored=self.archive(version=3,archived=False)
        self.assertEqual(corrected['corrections'],restored['corrections'])
        self.assertEqual(self.entry['work_image'],restored['work_image'])
        self.assertEqual(restored,Notebook(self.book.directory).get(restored['id']))

    def test_failed_archive_write_and_invalid_history_preserve_file(self):
        before=self.path.read_bytes()
        with patch('study.notebook.os.replace',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): self.archive()
        self.assertEqual(before,self.path.read_bytes())
        bad=deepcopy(self.entry);bad['archived_at']='2026-09-16T12:00:00+00:00'
        with self.assertRaises(ValueError): validate_entry(bad)
        self.assertEqual(before,self.path.read_bytes())


class PhotoUXAppTests(unittest.TestCase):
    setUp=fixtures.PhotoAppTests.setUp
    fill=fixtures.PhotoAppTests.fill

    def add_photos(self):
        self.question_image=prepare_image(colored());self.work_image=prepare_image(fixtures.sample())
        self.app.session_state['photo_image']=self.question_image
        self.app.session_state['photo_work_image']=self.work_image
        self.app.run();self.fill()

    def test_preview_is_local_and_apply_invalidates_old_analysis(self):
        self.add_photos()
        with patch('study.ui.service') as mocked:
            mocked.return_value.analyze.return_value=fixtures.new_solution()
            fixtures.button(self.app,'分析这道题').click().run()
        horizontal='photo_image-'+self.question_image['sha256']+'-horizontal'
        self.app.slider(key=horizontal).set_value((0,50)).run()
        self.assertTrue(self.app.checkbox(key='photo_confirmed').value)
        self.assertEqual(self.question_image,self.app.session_state['photo_image'])
        with patch('study.ui.service',side_effect=AssertionError('调整图片禁止联网')):
            fixtures.button(self.app,'应用题目照片调整').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
        self.assertIsNone(self.app.session_state['photo_analysis'])
        self.assertEqual(40,self.app.session_state['photo_image']['width'])
        self.assertEqual(self.question_image,self.app.session_state['photo_image_original'])
        self.assertFalse((self.folder/'notebook').exists())
        fixtures.button(self.app,'恢复题目照片原图').click().run()
        self.assertEqual(self.question_image,self.app.session_state['photo_image'])

    def test_save_two_photos_and_restore_after_restart(self):
        self.add_photos()
        fixtures.button(self.app,'确认加入错题本').click().run()
        book=Notebook(self.folder/'notebook');records,_=book.list()
        self.assertEqual(self.question_image,records[0]['image']);self.assertEqual(self.work_image,records[0]['work_image'])
        fresh=AppTest.from_file(str(fixtures.ROOT/'app.py'),default_timeout=15).run()
        fresh.radio(key='study_view').set_value('错题本').run()
        self.assertFalse(fresh.exception)
        self.assertTrue(any(e.label=='查看作答照片' for e in fresh.expander))

    def test_remove_work_photo_requires_reconfirmation(self):
        self.add_photos()
        fixtures.button(self.app,'移除作答照片').click().run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.checkbox(key='photo_confirmed').value)
        self.assertIsNone(self.app.session_state['photo_work_image'])
        self.assertEqual(self.question_image,self.app.session_state['photo_image'])

    def test_trash_restore_without_losing_records(self):
        self.add_photos();fixtures.button(self.app,'确认加入错题本').click().run()
        book=Notebook(self.folder/'notebook');original=book.list()[0][0]
        self.app.radio(key='study_view').set_value('错题本').run()
        fixtures.button(self.app,'移入回收站').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual([],book.list()[0]);self.assertEqual(1,len(book.list(archived=True)[0]))
        self.app.radio(key='notebook_collection').set_value('回收站').run()
        fixtures.button(self.app,'恢复这道题').click().run()
        self.assertFalse(self.app.exception)
        restored=book.list()[0][0]
        self.assertEqual(original['image'],restored['image']);self.assertEqual(original['work_image'],restored['work_image'])
        self.assertEqual([],book.list(archived=True)[0])


if __name__=='__main__': unittest.main(verbosity=2)
