"""三问读题卡的 Streamlit 展示；练习留在会话中，日志与已确认进度分开。"""
from dataclasses import asdict
from uuid import uuid4

import streamlit as st

from reading_check import READING_TASKS, READING_VERSION, check_reading, reading_questions


def render_reading_card(workspace, task_id):
    if task_id not in READING_TASKS:
        return
    # 控件会在切题时被 Streamlit 清理；另存每道题的练习，回来时恢复自己的选择。
    practice = st.session_state.setdefault('reading_practice', {})
    entry = practice.setdefault(task_id, {'selections': {}, 'report': None, 'warning': None})
    with st.expander('先读懂题意：三问读题卡（可选）', expanded=True):
        st.caption('先选出整体和所求量，再列式。检查选择不会更新学习进度。')
        selected = {}
        with st.form(f'reading-form-{task_id}'):
            for question in reading_questions(task_id):
                labels = {None: '先选一项', **dict(question.choices)}
                widget_key = f'reading-{task_id}-{question.key}'
                if widget_key not in st.session_state:
                    st.session_state[widget_key] = entry['selections'].get(question.key)
                selected[question.key] = st.selectbox(question.prompt, list(labels),
                                                       format_func=labels.get, key=widget_key, placeholder='先选一项')
            submitted = st.form_submit_button('检查我的读题')
        if submitted:
            report = check_reading(task_id, selected)
            entry.update(selections=report.selections, report=report, warning=None)
            try:
                workspace.log({'event': 'reading_check', 'reading_version': READING_VERSION,
                               'request_id': str(uuid4()), 'task_id': task_id,
                               'reading_check': asdict(report), 'real_api_calls': 0})
            except OSError:
                entry['warning'] = '本次读题记录未能保存；检查结果仍可查看，学习进度未改变。'
        report = entry['report']
        if report:
            st.caption('下次求助将携带以下已检查的选择，帮助老师定位哪一问需要解释。修改后请再次检查。')
            for row in report.rows:
                st.write(row['question'])
                st.caption('上次选择：' + (row['selected_text'] or '未选择'))
                show = st.success if row['status'] == 'correct' else st.warning if row['status'] == 'incorrect' else st.info
                show(row['feedback'])
            st.info(report.next_prompt)
            st.caption(report.verified_scope + '。')
        if entry['warning']:
            st.warning(entry['warning'])
