"""工具循环接入主页面；开关、缓存、候选和用户决定仍由程序管理。"""
import streamlit as st

from study.agent_tools import ToolScope
from study.agent_audit import AgentAudit, assert_candidate, record_decision
from study.agent_protocol import VERSION
from study.run_audit import digest
from study.demo_service import enabled as demo_enabled


def enabled():
    return bool(st.session_state.get('study_agent_enabled')) and not demo_enabled()


def scope(notebook, exclude_id=None):
    return ToolScope(notebook.directory, history_enabled=bool(st.session_state.get('study_agent_history')),
                     exclude_id=exclude_id)


def audit(notebook):
    return AgentAudit(notebook.directory.parent/'agent-runs')


def signature(notebook, exclude_id=None):
    if not enabled(): return ''
    try: selected=scope(notebook,exclude_id).signature()
    except (OSError,ValueError): selected={'invalid_sources':True,'history':bool(st.session_state.get('study_agent_history'))}
    return digest([VERSION,selected])


def perform(notebook, request_key, method, *args, **kwargs):
    from study.ui import service
    tutor=service()
    if enabled():
        from study.agent import AgentStudyService
        excluded=args[0]['id'] if method=='reanalyze' else None
        transport=tutor.transport if (tutor.transport.kind=='local_http_test' or
            tutor.transport.endpoint==tutor.config.base_url+'/chat/completions') else None
        tutor=AgentStudyService(tutor.config,tutor.key,transport=transport,scope=scope(notebook,excluded),
                                audit_dir=audit(notebook).directory)
    try:
        value=getattr(tutor,method)(*args,**kwargs)
        return {'value':value,'agent_run':tutor.run_id if enabled() else None}
    finally:
        if enabled() and getattr(tutor,'last_run',None):
            st.session_state.study_agent_last_run={'request_key':request_key,'run_id':tutor.run_id}


def before_save(notebook, active, result, exclude_id=None):
    if active.get('agent_run'):
        assert_candidate(audit(notebook),active['agent_run'],result,scope(notebook,exclude_id))


def after_save(notebook, active, saved_entry, operation_id=None):
    if active.get('agent_run'):
        try:
            record_decision(audit(notebook),active['agent_run'],'accept',saved_entry=saved_entry,operation_id=operation_id)
        except (OSError,ValueError):
            st.session_state.study_agent_save_notice='题目已保存，但资料查询记录未更新；请核对本机执行记录。'


def reject(notebook, active):
    if active.get('agent_run'): record_decision(audit(notebook),active['agent_run'],'reject')


def discard_candidate(notebook, active, request_key, entry_id=None):
    # 在 Streamlit 回调阶段清除，避免同一轮按钮状态在 rerun 时恢复候选。
    try:
        reject(notebook,active)
        if entry_id:
            st.session_state.correction_drafts.pop(entry_id,None)
            if active.get('agent_run'):
                st.session_state.correction_calls[request_key]={'error':'本次分析已放弃；重新请求前请手动清除记录。'}
        else:
            st.session_state.photo_analysis=None
            st.session_state.photo_calls[request_key]={'error':'本次分析已放弃；如需重新请求，请先手动清除记录。'}
    except (OSError,ValueError) as exc:
        st.session_state.study_agent_action_error=str(exc)


def show_trace(notebook, request_key):
    latest=st.session_state.get('study_agent_last_run')
    if not enabled() or not latest or latest['request_key']!=request_key: return
    try: value=audit(notebook).read(latest['run_id'])
    except (OSError,ValueError):
        st.warning('本次执行记录暂时无法读取。');return
    labels={'success':'完成，待你核对','needs_clarification':'需要补充条件','budget_exhausted':'达到次数或时间限制',
            'tool_failed':'资料查询失败','failed':'回复未通过检查','running':'未完成，不自动重发'}
    with st.expander('本次资料查询记录'):
        st.write(labels.get(value['status'],value['status']))
        st.caption(f"模型请求 {len(value['model_calls'])}/{value['limits']['model_requests']}；"
                   f"工具请求 {value['tool_requests_seen']}/{value['limits']['tool_attempts']}。费用未知，以供应商账单为准。")
        for call in value['model_calls']:
            st.text(call['request_id']+' · '+call['status'])
        for item in value['tool_calls']:
            st.write(item['name']+' · '+item['status']);st.json(item['arguments'])
            for source in (item.get('result') or {}).get('sources',[]):
                st.caption(source['source_id']+' · '+source['kind']);st.text(source['snippet'])
        for citation in (value['final'] or {}).get('citations',[]):
            st.text(citation['source_id']+'：'+citation['quote'])
        if value['error_code']: st.caption('停止原因：'+value['error_code'])
        if value['decision']: st.caption('你的决定：'+value['decision']['action'])
        st.caption('记录只包含请求行为、资料来源和结果，不包含模型内部思维链。引用可追溯不等于内容已经正确。')
