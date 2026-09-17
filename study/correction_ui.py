"""已收藏题目的订正预览、确认、放弃与历史；请求和落盘分开。"""
from uuid import uuid4
import streamlit as st

from study.corrections import baseline, fingerprint, CHANGE_LABELS
from study.diagnosis import WORK_KINDS


def unconfirm(key):
    st.session_state[key]=False


def discard(entry_id):
    st.session_state.correction_drafts.pop(entry_id,None)


def show_comparison(result,*,example=False):
    st.markdown('**前后有什么变化**')
    st.write(result['comparison']['summary'])
    for row in result['comparison']['changes']:
        with st.container(border=True):
            st.write(CHANGE_LABELS[row['status']])
            previous,current=st.columns(2)
            with previous:
                st.caption('之前的作答')
                st.text(row['previous_excerpt'])
            with current:
                st.caption('本次订正')
                st.text(row['current_excerpt'])
            st.write(row['explanation'])
    if not result['comparison']['changes']:
        st.caption('本次未提供逐步变化对照；可展开本次分析，结合前后作答核对。')
    st.info('接下来做一步：'+result['analysis']['next_practice'])
    st.caption('人工编写的订正示例，不是模型实测。' if example else '变化判断来自模型，不自动记录独立做对或已掌握。')


def render(notebook,entry):
    from study.ui import service, run_once, show_analysis, reply_origin
    from study.demo_service import enabled as demo_enabled, fill_correction, ORIGIN as DEMO_ORIGIN
    st.subheader('订正后再分析')
    st.caption('写下新的完整作答，再和之前保存的作答对照。分析完成后，由你决定是否保存这一轮。')
    entry_id=entry['id']
    answer_key='correction-answer-'+entry_id
    kind_key='correction-kind-'+entry_id
    confirm_key='correction-confirm-'+entry_id
    drafts=st.session_state.setdefault('correction_drafts',{})
    if st.session_state.pop('correction-reset-'+entry_id,False):
        for key in (answer_key,kind_key,confirm_key): st.session_state.pop(key,None)
        st.success('本次订正与分析已保存。原作答和自评记录保持原样。')
    previous=baseline(entry)
    label='原作答' if previous['id']=='original' else '上一次已保存的订正'
    with st.expander('本次对照：'+label):
        st.text(previous['work'] or '此前没有记录作答。')
    if demo_enabled():
        st.button('填入演示订正',on_click=fill_correction,args=(entry_id,),
                  disabled=bool(st.session_state.get(answer_key) or entry.get('corrections')))
    answer=st.text_area('本次订正过程',key=answer_key,max_chars=3000,height=130,
        placeholder='每行写一步，保留完整列式和结果。',on_change=unconfirm,args=(confirm_key,))
    kind=st.radio('这次提供了什么',['unclear','answer_only','steps'],format_func=WORK_KINDS.get,
                  key=kind_key,horizontal=True,on_change=unconfirm,args=(confirm_key,))
    confirmed=st.checkbox('本次作答与作答类型已核对',key=confirm_key)
    current=fingerprint(entry,answer,kind) if answer.strip() else None
    from study import agent_ui,context_ui
    if current: current+=agent_ui.signature(notebook,entry_id)
    options=context_ui.options(notebook,'correction-'+entry_id,str(current),entry_id)
    from study.run_audit import digest
    if current:current+=digest(options['settings'])
    base='correction-v4-'+str(current)+'-'+reply_origin()
    request_key=context_ui.analysis_key(options,base,'compare')
    if st.button('分析本次订正',type='primary',disabled=not(answer.strip() and confirmed)):
        try:
            with st.spinner('正在核对本次作答与前后变化…'):
                extra={} if demo_enabled() else {'learning':context_ui.learning(options,'compare')}
                packet=run_once(request_key,lambda:agent_ui.perform(notebook,request_key,'reanalyze',entry,answer,work_kind=kind,**extra),cache_name='correction_calls')
            old=drafts.get(entry_id)
            drafts[entry_id]={'fingerprint':current,'result':packet['value'],'agent_run':packet['agent_run'],'based_on':previous['id'],
                'operation_id':old['operation_id'] if old and old['fingerprint']==current else uuid4().hex,
                'origin':reply_origin(),'request_key':request_key,'created_at':packet['created_at']}
            context_ui.analyzed(options,base,request_key,packet['value'],reply_origin())
            st.rerun()
        except (ValueError,OSError) as exc: st.error(str(exc))
    agent_ui.show_trace(notebook,request_key)
    st.caption('离线回放人工编写的订正对照，不调用模型；确认后才保存到演示错题本。' if demo_enabled() else
               '点击分析才发送本题照片、前后作答和此前分析，可能计费；分析不会自动保存。无需模型时可在下方手动记录复习。')
    pending=drafts.get(entry_id)
    active=pending if pending and pending['fingerprint']==current else None
    if pending and not active: st.info('作答、类型或本题版本已变化，旧订正分析不能保存，请重新核对并分析。')
    if active:
        context_ui.seed_analysis(options,active,active['result'])
        show_comparison(active['result'],example=active['origin']==DEMO_ORIGIN)
        with st.expander('查看本次完整分析'): show_analysis(active['result']['analysis'],example=active['origin']==DEMO_ORIGIN)
        accept,reject=st.columns(2)
        if accept.button('确认保存本次订正',type='primary',disabled=not confirmed):
            try:
                agent_ui.before_save(notebook,active,active['result'],entry_id)
                saved_entry=notebook.add_correction(entry_id,entry['version'],active['operation_id'],based_on=active['based_on'],
                    answer=answer,work_kind=kind,result=active['result'],analysis_origin=active['origin'])
                agent_ui.after_save(notebook,active,saved_entry,active['operation_id'])
                drafts.pop(entry_id,None)
                st.session_state['correction-reset-'+entry_id]=True
                st.rerun()
            except (ValueError,OSError) as exc: st.error(f'没有保存：{exc}')
        reject.button('不保存本次分析',on_click=agent_ui.discard_candidate,
                      args=(notebook,active,active['request_key'],entry_id))
    context_ui.render_coach(notebook,options,entry['question'],entry['level'],answer,entry['image'],kind=kind,
        work_image=entry.get('work_image'),confirmed=bool(answer.strip() and confirmed),previous_entry=entry)
    if st.session_state.get('study_agent_action_error'):
        st.error(st.session_state.pop('study_agent_action_error'))
    cached=st.session_state.get('correction_calls',{}).get(request_key,{})
    if 'error' in cached and st.button('清除此订正的失败记录'):
        st.session_state.correction_calls.pop(request_key,None)
        st.rerun()
    history=entry.get('corrections',[])
    if history:
        st.markdown(f'**已保存 {len(history)} 次订正**')
        for index,row in reversed(list(enumerate(history,1))):
            with st.expander(f"第 {index} 次订正 · {row['at'][:10]}"):
                st.text(row['answer'])
                st.caption(row['analysis_origin'])
                show_comparison(row['result'],example=row['analysis_origin']==DEMO_ORIGIN)
                show_analysis(row['result']['analysis'],example=row['analysis_origin']==DEMO_ORIGIN)
    st.divider()
