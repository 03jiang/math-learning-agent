"""启动：python -m streamlit run app.py --server.address 127.0.0.1。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
from dataclasses import asdict
import json
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st

from legacy.core import SETTING_OPTIONS, ValidationError
from legacy.workflow import TASKS, Workspace
from legacy.curriculum import QUESTIONS, OPERANDS
from legacy.model_api import configured_tutor
from legacy.reading_ui import render_reading_card

st.set_page_config(page_title='数学学习助手', page_icon='🌱', layout='wide')
DATA_DIR = Path(os.environ.get('MATH_ASSISTANT_DATA_DIR', Path(__file__).resolve().parents[1] / 'data' / 'math'))
# 切换到错题本时仍保留当前题目的输入；Streamlit 未显示控件时会清理控件键。
for draft_key in ('photo_question', 'photo_work', 'photo_work_kind', 'photo_level', 'photo_confirmed', 'photo_source'):
    if draft_key in st.session_state:
        st.session_state[draft_key] = st.session_state[draft_key]
for draft_key in list(st.session_state):
    if draft_key.startswith(('correction-answer-','correction-kind-','correction-confirm-')):
        st.session_state[draft_key]=st.session_state[draft_key]
SPACES = ['四题练习']
start_view = os.environ.get('MATH_ASSISTANT_START_VIEW', '拍照解题')
view = st.radio('学习空间', SPACES, index=SPACES.index(start_view) if start_view in SPACES else 0,
                horizontal=True, key='study_view')
if view != '四题练习':
    from study.ui import render
    render(DATA_DIR / 'notebook', view)
    st.stop()
LABELS = {'step_size': '步骤大小', 'explanation_mode': '解释方式', 'presentation_density': '信息密度',
          'structure_level': '组织方式', 'pattern_guidance': '方法提示', 'scope_support': '知识联系'}
VALUES = {'small': '小步骤', 'medium': '中等步骤', 'large': '完整步骤',
          'hint': '提示', 'example': '示例', 'direct': '直接帮助', 'brief': '简洁', 'detailed': '详细',
          'free': '自然说明', 'guided': '分项引导', 'off': '不附加方法', 'on': '附加通用方法',
          'focused': '聚焦本题', 'connected': '联系一个知识点'}
OPTIONS = {name: list(values) for name, values in SETTING_OPTIONS.items()}
HELPS = {'step_size': '一次提示当前一步、两步或剩余步骤；直接帮助会给出完整计算。',
         'explanation_mode': '选择小提示、具体示例或完整计算解释。',
         'presentation_density': '详细模式会补充概念解释，也会展开方法和知识联系。',
         'structure_level': '分项引导按目标、说明、句子填空组织当前步骤。',
         'pattern_guidance': '附加可复用的方法，例如求剩余的整体与部分关系。',
         'scope_support': '可选联系数轴或实际数量；不会切换题目或修改进度。'}

if 'workspace' not in st.session_state or st.session_state.get('app_revision') != 'math-deepseek-ready-v1':
    try:
        st.session_state.workspace = Workspace(DATA_DIR, tutor=configured_tutor())
        st.session_state.latest = {}
        st.session_state.settings_pending = {}
        st.session_state.decisions = {}
        st.session_state.app_revision = 'math-deepseek-ready-v1'
    except (OSError, ValueError) as exc:
        st.error(f'初始化失败：{exc}。配置和存档没有被覆盖。')
        st.stop()
workspace = st.session_state.workspace

with st.sidebar:
    st.title('🌱 小学数学工作台')
    task_id = st.selectbox('当前任务', list(TASKS), format_func=TASKS.get, key='selected_task')
    st.caption('每个任务独立保存进度与长期设置。')
    st.divider()
    if getattr(workspace.tutor, 'uses_http', False):
        if workspace.tutor.transport.kind == 'real_api':
            st.markdown('**分数与应用题 · 真实模型**')
            st.caption(f'当前服务：{workspace.tutor.name}。提交会发送本次问题、当前任务和相关笔记，可能产生费用。')
        else:
            st.markdown('**分数与应用题 · 本机接口测试**')
            st.caption('使用手写 HTTP 响应，真实 API 调用为 0。')
    else:
        st.markdown('**分数与应用题 · 本地模拟**')
        st.caption('只读检索 8 篇自写笔记。当前为模拟模式，不会调用真实模型。')

assistant = workspace.assistant(task_id)
reset_task = st.session_state.pop('reset_question_form', None)
if reset_task is not None:
    # 在创建控件前重置，确保只生效一轮；不依赖浏览器端 clear_on_submit。
    st.session_state[f'message-{reset_task}'] = ''
    st.session_state[f'answer-{reset_task}'] = ''
    st.session_state[f'steps-{reset_task}'] = ''
    st.session_state[f'notes-{reset_task}'] = ''
    for name in OPTIONS:
        st.session_state[f'temp-{reset_task}-{name}'] = 'inherit'
try:
    snapshot = assistant.snapshot()
except (OSError, ValueError) as exc:
    st.error(f'读取任务失败：{exc}')
    st.stop()

st.title(TASKS[task_id])
st.info(QUESTIONS[task_id])
with st.expander('看一看：同样大小的整体，平均分成不同的份数'):
    left, right = OPERANDS[task_id]
    st.caption(f'下面两条长条一样长。蓝色部分分别表示 {left} 和 {right}，白色部分仍属于这个整体。')
    for fraction in (left, right):
        numerator, denominator = fraction.numerator, fraction.denominator
        width = 240 / denominator
        boxes = ''.join(f'<rect x="{i * width}" y="2" width="{width}" height="36" '
                        f'fill="{"#4b85c3" if i < numerator else "#ffffff"}" stroke="#52677b" />'
                        for i in range(denominator))
        st.markdown(f'<div style="max-width:360px;margin:12px 0">{numerator}/{denominator}'
                    f'<svg viewBox="-1 0 242 40" role="img" aria-label="平均分成{denominator}份，取{numerator}份">'
                    f'{boxes}</svg></div>', unsafe_allow_html=True)
st.caption('先获得帮助，再由你决定是否更新学习进度。')
if not getattr(workspace.tutor, 'uses_http', False):
    st.info('模拟模式 · 使用本地规则生成帮助，未连接真实模型。')
main = st.container()


def render_pending(proposal, title):
    if proposal is None:
        return
    st.subheader(title)
    state_patch = proposal.proposed_state_update
    config_patch = proposal.proposed_configuration_update
    if state_patch is not None:
        st.caption('接受当前步骤只表示接下来做什么，不表示已经完成。')
        st.write(f"建议当前步骤：{state_patch.get('current_step', '见详细更新')}")
    if config_patch is not None:
        for name, value in config_patch.items():
            st.write(f'{LABELS[name]}：{VALUES[value]}')
    if proposal.proposal_id in st.session_state.decisions:
        status, warning = st.session_state.decisions[proposal.proposal_id]
        st.success({'applied': '已保存确认更新', 'rejected': '已拒绝，任务状态未改变',
                    'already_applied': '这项建议已经保存过'}[status])
        if warning:
            st.warning(warning)
        return
    with st.form(f'decision-{proposal.proposal_id}'):
        if state_patch is not None and set(state_patch) == {'current_step'}:
            edited_state = {'current_step': st.text_input('编辑当前步骤（可选）', value=state_patch['current_step'],
                                                          key=f'edit-{proposal.proposal_id}')}
        elif state_patch is not None:
            edited_json = st.text_area('详细更新内容（JSON）', value=json.dumps(state_patch, ensure_ascii=False, indent=2))
        if config_patch is not None:
            edited_config = {name: st.selectbox(f'编辑{LABELS[name]}', OPTIONS[name], index=OPTIONS[name].index(value),
                                                format_func=VALUES.get, key=f'edit-{proposal.proposal_id}-{name}')
                             for name, value in config_patch.items()}
        accept = st.form_submit_button('接受更新')
        edit = st.form_submit_button('保存编辑后的更新')
        reject = st.form_submit_button('拒绝更新')
    if accept or edit or reject:
        try:
            kwargs = {}
            if edit:
                if state_patch is not None:
                    patch = edited_state if set(state_patch) == {'current_step'} else json.loads(edited_json)
                    if type(patch) is not dict or not patch:
                        raise ValidationError('编辑内容必须是非空对象')
                    kwargs['edited_state'] = patch
                if config_patch is not None:
                    kwargs['edited_configuration'] = edited_config
            action = 'reject' if reject else 'edit' if edit else 'accept'
            status, warning = workspace.decide(task_id, proposal, action, **kwargs)
            st.session_state.decisions[proposal.proposal_id] = (status, warning)
            st.rerun()
        except (OSError, ValueError) as exc:
            st.error(f'未保存更新：{exc}')


with main:
    render_reading_card(workspace, task_id)
    st.subheader('这一步卡在哪里？')
    with st.form(f'question-{task_id}', clear_on_submit=True):
        message = st.text_area('描述当前困难', placeholder='我不明白为什么要通分，请先给一个小提示。',
                               key=f'message-{task_id}', max_chars=2000)
        answer_input = st.text_input('我的答案（可选）', placeholder='例如 3/4；想法写在上面', key=f'answer-{task_id}')
        solution_steps = st.text_area('我的解题步骤（可选，每行一步）',
                                      placeholder='例如：\n1/2 = 2/4\n2/4 + 1/4 = 3/4',
                                      key=f'steps-{task_id}', max_chars=2000)
        st.caption('可以只提交步骤。支持分数等式；文字解释会保留，并标明尚未自动核对。')
        overrides = {}
        with st.expander('本轮设置（可选）', expanded=False):
            st.caption('仅影响这次求助，提交后恢复为沿用长期设置。')
            items = list(OPTIONS.items())
            for start in range(0, len(items), 2):
                for col, (name, choices) in zip(st.columns(2), items[start:start + 2]):
                    with col:
                        selected = st.selectbox(LABELS[name], ['inherit'] + choices,
                                                format_func=lambda x: '沿用长期设置' if x == 'inherit' else VALUES[x],
                                                help=HELPS[name], key=f'temp-{task_id}-{name}')
                        if selected != 'inherit':
                            overrides[name] = selected
        with st.expander('查阅学习笔记（可选）'):
            search_query = st.text_input('笔记关键词', placeholder='例如：通分、整体、剩余', key=f'notes-{task_id}', max_chars=200)
            st.caption('填写关键词后，随本次求助查阅自写笔记。')
        submitted = st.form_submit_button('提交求助', type='primary')
        actions = st.columns(3)
        hint = actions[0].form_submit_button('给个小提示')
        direct = actions[1].form_submit_button('完整讲解')
        next_step = actions[2].form_submit_button('建议下一步')
        help_action = 'hint' if hint else 'direct' if direct else 'next' if next_step else None
        submitted = submitted or bool(help_action)
    if submitted:
        try:
            reading_report = st.session_state.get('reading_practice', {}).get(task_id, {}).get('report')
            with st.spinner('正在检查并生成帮助…'):
                result = workspace.run(str(uuid4()), task_id, message, overrides,
                                       answer_submission=answer_input if answer_input.strip() else None,
                                       solution_steps=solution_steps, help_action=help_action,
                                       search_query=search_query.strip() or None,
                                       reading_selections=reading_report.selections if reading_report else None)
            st.session_state.latest[task_id] = result.request_id
            st.session_state.reset_question_form = task_id
            st.rerun()
        except (OSError, ValueError) as exc:
            st.error(str(exc))
    request_id = st.session_state.latest.get(task_id)
    if request_id:
        result = workspace.rounds[request_id]
        if result.log_warning:
            st.warning(result.log_warning)
        if result.error:
            st.error(f'本轮未生成帮助或更新建议：{result.error}')
        # 数学校验独立于教学回复，检索或外部回复失败时也不丢失。
        answer_check = result.local_checks.get('answer')
        step_check = result.local_checks.get('steps')
        if answer_check:
            st.subheader('本地答案检查')
            feedback = answer_check['feedback']
            if answer_check['status'] == 'correct' and step_check and step_check['status'] == 'incorrect':
                feedback = '最终答案数值相符，但解题步骤中仍有错误，请先修正。'
            st.info(feedback)
            st.caption('由本地程序核对数值，不自动记录为已掌握。')
        if step_check:
            st.subheader('逐步检查')
            for row in step_check['rows']:
                st.write(f"第 {row['line']} 步：{row['submitted']}")
                if row['status'] == 'incorrect':
                    st.error(row['feedback'])
                elif row['status'] == 'correct':
                    st.success(row['feedback'])
                else:
                    st.info(row['feedback'])
            st.caption('等式核对通过不代表已掌握；下一步仍由你确认。')
        if result.reply:
            st.divider()
            st.subheader('本轮帮助')
            st.caption(result.reply.source)
            external = result.reply.origin in {'real_api', 'local_http_test'}
            (st.text if external else st.write)(result.reply.explanation)
            if result.reply.cited_source_ids:
                st.caption('本轮回复引用：' + '、'.join(result.reply.cited_source_ids))
            st.markdown('**下一步**')
            st.write(result.reply.next_action)
            if result.reply.optional_hint:
                (st.text if external else st.info)(result.reply.optional_hint)
            if result.sources:
                hint_mode = result.effective_settings['explanation_mode'] == 'hint'
                if hint_mode:
                    st.caption('参考笔记可能包含完整解答，需要时再展开。')
                with st.expander(f'参考笔记 · {len(result.sources)} 个来源', expanded=not hint_mode):
                    st.caption('以下为只读参考片段，按标题、标签和关键词匹配。')
                    for source in result.sources:
                        st.markdown(f"**{source['source_id']} · {source['title']}**")
                        st.text(source['snippet'])
            render_pending(result.proposal, '待确认的进度更新')
        with st.expander('本轮运行记录'):
            st.json({'有效设置': result.effective_settings, '工具调用': result.tool_records,
                     '生成轮数（当前会话）': workspace.generation_count,
                     '真实 API 请求尝试数': result.real_api_calls, '模型调用记录': result.model_calls})

with st.sidebar:
    st.subheader('已确认进度')
    st.write(snapshot.task.objective)
    st.markdown('**当前步骤**')
    st.write(snapshot.task.current_step)
    st.markdown('**已完成**')
    for step in snapshot.task.completed_steps:
        st.write(f'✓ {step}')
    if not snapshot.task.completed_steps:
        st.caption('尚未记录已完成步骤。')
    st.caption(f'状态版本：{snapshot.metadata.version}')
    with st.expander('完整任务状态'):
        st.json(asdict(snapshot.task))
    st.divider()
    st.subheader('长期设置')
    st.caption('只影响当前任务；提交建议后还需明确确认。')
    with st.form(f'settings-{task_id}'):
        chosen = {}
        for name, choices in OPTIONS.items():
            chosen[name] = st.selectbox(LABELS[name], choices, index=choices.index(getattr(snapshot.settings, name)),
                                        format_func=VALUES.get, help=HELPS[name],
                                        key=f'long-{task_id}-{name}-{snapshot.metadata.version}')
        save_settings = st.form_submit_button('生成长期设置建议')
    if save_settings:
        changes = {key: value for key, value in chosen.items() if value != getattr(snapshot.settings, key)}
        if changes:
            old = st.session_state.settings_pending.get(task_id)
            if old and old.proposal_id not in st.session_state.decisions:
                assistant.reject(old)
            st.session_state.settings_pending[task_id] = assistant.propose(configuration_patch=changes)
        else:
            st.info('设置没有变化。')
    render_pending(st.session_state.settings_pending.get(task_id), '待确认的长期设置')
