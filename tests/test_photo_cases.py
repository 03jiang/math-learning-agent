"""测试集覆盖与生成完整性；不把生成成功当作模型识图或数学正确率。"""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from study.images import prepare_image
from study.photo_cases import CASES, MANUAL_CASES, build


class PhotoCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.folder=Path(cls.temp.name)/'suite'
        with patch('socket.socket',side_effect=AssertionError('生成测试图片禁止联网')):
            cls.manifest=build(cls.folder)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_images_are_uploadable_hashes_match_and_rotation_is_corrected(self):
        self.assertEqual(13,len(self.manifest['cases']))
        for case in self.manifest['cases']:
            raw=(self.folder/case['image']).read_bytes()
            self.assertEqual(case['sha256'],sha256(raw).hexdigest())
            normalized=prepare_image(raw)
            self.assertEqual((1200,1000),(normalized['width'],normalized['height']))
        with Image.open(self.folder/'images/q12_rotated_image.jpg') as image:
            self.assertEqual(6,image.getexif()[274])

    def test_cases_cover_three_levels_and_evidence_limits(self):
        self.assertEqual({'小学','初中','高中'},{case['level'] for case in CASES})
        self.assertEqual(len(CASES),len({case['id'] for case in CASES}))
        self.assertTrue(any(c['work_kind']=='answer_only' for c in CASES))
        self.assertTrue(any(c['status']=='needs_clarification' and c['answer'] is None for c in CASES))
        self.assertTrue(any('等价正确解法' in c['tags'] for c in CASES))
        self.assertTrue(any('教师批注' in c['tags'] for c in CASES))
        self.assertTrue(any('模糊作答' in c['tags'] and c['work_kind']=='unclear' for c in CASES))
        for case in CASES:
            self.assertTrue(case['checks'])
            self.assertEqual(case['work_kind']=='none',not case['student_work'])

    def test_real_photos_and_all_scores_are_explicitly_unrun(self):
        self.assertEqual(0,self.manifest['real_api_calls'])
        self.assertEqual('synthetic_images_not_real_photos',self.manifest['evidence_kind'])
        self.assertTrue(all(c['evaluation_status']=='not_run' for c in self.manifest['cases']))
        self.assertTrue(all(c['image'] is None and c['status']=='awaiting_image' for c in MANUAL_CASES))
        scoring=json.loads((self.folder/'score-template.json').read_text())
        self.assertEqual(13,len(scoring['rows']))
        self.assertEqual('unscored',scoring['status'])
        self.assertIn('| 图片 |\n|---|---|---|\n| p01_', (self.folder/'README.md').read_text())
        for row in scoring['rows']:
            for key in ('ocr','separation','math','evidence_boundary','next_step'): self.assertIsNone(row[key])

    def test_existing_suite_and_scores_are_never_overwritten(self):
        before={p.name:sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir() if p.is_file()}
        with self.assertRaises(ValueError): build(self.folder)
        self.assertEqual(before,{p.name:sha256(p.read_bytes()).hexdigest() for p in self.folder.iterdir() if p.is_file()})


if __name__=='__main__': unittest.main(verbosity=2)
