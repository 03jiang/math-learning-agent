"""新版入口：python -m study.launch --demo。"""
import os
from pathlib import Path
import streamlit as st
from study.ui import render

st.set_page_config(page_title='数学错题助手', page_icon='🌱', layout='wide')
DATA_DIR=Path(os.environ.get('MATH_ASSISTANT_DATA_DIR',Path(__file__).resolve().parent/'data'/'study'))
# 切换到错题本时仍保留当前题目的输入；Streamlit 未显示控件时会清理控件键。
for draft_key in ('photo_question', 'photo_work', 'photo_work_kind', 'photo_level', 'photo_confirmed', 'photo_source'):
    if draft_key in st.session_state:
        st.session_state[draft_key] = st.session_state[draft_key]
for draft_key in list(st.session_state):
    if draft_key.startswith(('correction-answer-','correction-kind-','correction-confirm-')):
        st.session_state[draft_key]=st.session_state[draft_key]
SPACES=['拍照解题','错题本','学习回顾']
start_view=os.environ.get('MATH_ASSISTANT_START_VIEW','拍照解题')
view=st.radio('学习空间',SPACES,index=SPACES.index(start_view) if start_view in SPACES else 0,
              horizontal=True,key='study_view')
render(DATA_DIR/'notebook',view)

st.stop()
