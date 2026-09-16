"""固定一题的离线回放；严格匹配输入，不把示例当作任意题目的模型回复。"""
from copy import deepcopy
import os
from pathlib import Path

from study.example import QUESTION, STUDENT_WORK, ANALYSIS, corrected_example
from study.images import prepare_image, image_bytes
from study.notebook import validate_entry, ensure_active
from study.diagnosis import validate_analysis
from study.corrections import validate_result, baseline

ORIGIN = '离线演示 / 人工编写的固定方程题'
ROOT = Path(__file__).resolve().parents[1]


def enabled():
    return os.environ.get('MATH_STUDY_DEMO') == '1'


def sample_image():
    return prepare_image((ROOT/'evaluation/photo_cases_v1/images/m04_equation_wrong.png').read_bytes())


def check_photos(image, work_image):
    expected = sample_image()['sha256']
    for photo in (image, work_image):
        if photo is not None:
            image_bytes(photo)
            if photo['sha256'] != expected:
                raise ValueError('离线演示只支持自带方程题原图；任意照片需在普通入口连接模型。')


class DemoStudyService:
    def recognize(self, image=None, work_image=None, *, question_text=''):
        check_photos(image, work_image)
        if image is None and (work_image is None or question_text != QUESTION):
            raise ValueError('请先载入演示题。离线回放不会识别新的题目。')
        return {'text':QUESTION, 'student_work':STUDENT_WORK, 'work_kind':'steps',
                'warnings':['离线演示：以上是人工预设转录，不是 OCR 或真实模型结果。']}

    def analyze(self, question, level, my_work='', image=None, *, work_kind=None, work_image=None):
        check_photos(image, work_image)
        if (question,level,my_work,work_kind) != (QUESTION,'初中',STUDENT_WORK,'steps'):
            raise ValueError('离线演示只回放自带方程题及其原作答，修改后的任意输入不会得到模拟分析。')
        return validate_analysis(deepcopy(ANALYSIS),student_work=my_work,work_kind=work_kind)

    def reanalyze(self, entry, answer, *, work_kind):
        validate_entry(entry)
        ensure_active(entry)
        check_photos(entry['image'],entry.get('work_image'))
        expected,result = corrected_example()
        previous = baseline(entry)
        if (entry['question']!=QUESTION or entry['level']!='初中' or previous['id']!='original'
                or previous['work']!=STUDENT_WORK or previous['analysis']!=ANALYSIS
                or answer!=expected or work_kind!='steps'):
            raise ValueError('离线演示只包含原错答到自带订正的一轮对照；其他作答需真实模型。')
        return validate_result(result,previous_work=previous['work'],previous_analysis=previous['analysis'],
                               answer=answer,work_kind=work_kind)


def load_draft():
    import streamlit as st
    from study.ui import reset_draft, draft
    reset_draft()
    draft()
    st.session_state.update(photo_question=QUESTION,photo_work=STUDENT_WORK,photo_work_kind='steps',
                            photo_level='初中',photo_image=sample_image(),photo_confirmed=False)


def fill_correction(entry_id):
    import streamlit as st
    st.session_state['correction-answer-'+entry_id]=corrected_example()[0]
    st.session_state['correction-kind-'+entry_id]='steps'
    st.session_state['correction-confirm-'+entry_id]=False
