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
        st.caption('这次没有逐步对照结果。可以展开完整分析，再和前后的作答一起核对。')
    st.info('再试一步：'+result['analysis']['next_practice'])
    st.caption('这是人工编写的订正示例，不是实际模型回复。' if example else '这是模型对前后变化的判断，不会自动记为“独立做对”或“已掌握”。')


def render(notebook,entry):
    from study.ui import service, run_once, show_analysis, reply_origin
    from study.demo_service import enabled as demo_enabled, fill_correction, ORIGIN as DEMO_ORIGIN
    st.subheader('订正后再分析')
    st.caption('写下这次的完整解题过程，和上次保存的作答比较。看完分析后，再决定要不要保存。')
    entry_id=entry['id']
    answer_key='correction-answer-'+entry_id
    kind_key='correction-kind-'+entry_id
    confirm_key='correction-confirm-'+entry_id
    drafts=st.session_state.setdefault('correction_drafts',{})
    if st.session_state.pop('correction-reset-'+entry_id,False):
        for key in (answer_key,kind_key,confirm_key): st.session_state.pop(key,None)
        st.success('订正和分析已保存，原来的作答和自评记录没有改动。')
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
    request_key='correction-v2-'+str(current)+'-'+reply_origin()
    if st.button('分析本次订正',type='primary',disabled=not(answer.strip() and confirmed)):
        try:
            with st.spinner('正在核对本次作答与前后变化…'):
                result=run_once(request_key,lambda:service().reanalyze(entry,answer,work_kind=kind),cache_name='correction_calls')
            old=drafts.get(entry_id)
            drafts[entry_id]={'fingerprint':current,'result':result,'based_on':previous['id'],
                'operation_id':old['operation_id'] if old and old['fingerprint']==current else uuid4().hex,
                'origin':reply_origin()}
        except (ValueError,OSError) as exc: st.error(str(exc))
    st.caption('这里是人工编写的订正演示，不调用模型。只有确认后，才会存进演示错题本。' if demo_enabled() else
               '点击分析后，会发送本题照片、前后作答和此前分析，可能计费，但不会自动保存。也可以不使用模型，在下方手动记录复习。')
    pending=drafts.get(entry_id)
    active=pending if pending and pending['fingerprint']==current else None
    if pending and not active: st.info('作答、作答类型或已保存的记录有变化，之前的订正分析已失效。请重新核对并分析。')
    if active:
        show_comparison(active['result'],example=active['origin']==DEMO_ORIGIN)
        with st.expander('查看本次完整分析'): show_analysis(active['result']['analysis'],example=active['origin']==DEMO_ORIGIN)
        accept,reject=st.columns(2)
        if accept.button('确认保存本次订正',type='primary',disabled=not confirmed):
            try:
                notebook.add_correction(entry_id,entry['version'],active['operation_id'],based_on=active['based_on'],
                    answer=answer,work_kind=kind,result=active['result'],analysis_origin=active['origin'])
                drafts.pop(entry_id,None)
                st.session_state['correction-reset-'+entry_id]=True
                st.rerun()
            except (ValueError,OSError) as exc: st.error(f'没有保存：{exc}')
        reject.button('不保存本次分析',on_click=discard,args=(entry_id,))
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
