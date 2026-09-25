"""照片与订正页面共用的设置、临时要求及同题追问。"""
from copy import deepcopy
import streamlit as st

from legacy.core import SETTING_OPTIONS
from study import context
from study.preferences import LocalPreferences
from study.run_audit import digest
from study.demo_service import enabled as demo_enabled

LABELS={'step_size':'步骤大小','explanation_mode':'解释方式','presentation_density':'信息密度'}
VALUES={'small':'拆成小步','medium':'适中','large':'概括主要步骤','hint':'引导式说明',
        'example':'举同类例子','direct':'直接解释','brief':'简洁','detailed':'详细','inherit':'沿用长期设置'}


def sidebar(notebook):
    store=LocalPreferences(notebook.directory.parent);saved=store.load()
    with st.expander('长期学习设置'):
        st.caption('这台电脑、当前数据目录内跨题使用；点击保存才生效。不是自动推断的学习能力。')
        with st.form('study-settings-'+str(saved['version'])):
            values={k:st.selectbox(LABELS[k],SETTING_OPTIONS[k],index=SETTING_OPTIONS[k].index(v),
                format_func=VALUES.get,key='study-setting-'+k+'-'+str(saved['version']),disabled=demo_enabled())
                for k,v in saved['settings'].items()}
            if st.form_submit_button('确认保存长期设置',disabled=demo_enabled()):
                try:store.save(saved['version'],values);st.rerun()
                except (ValueError,OSError) as exc:st.error(str(exc))
        st.caption('目前接通这三项；本轮要求不会改动这里。离线固定题回放不响应设置。')


def options(notebook,slot,revision,exclude_id=None):
    from study import agent_ui
    policy=agent_ui.signature(notebook,exclude_id)
    identity=[str(notebook.directory.resolve()),slot]
    previous=st.session_state.get('study_context_active')
    task=context.task_for(previous,digest(identity),revision,policy)
    st.session_state.study_context_active=task
    saved=LocalPreferences(notebook.directory.parent).load()['settings']
    prefix='study-turn-'+task['identity'][:16]
    if task.pop('clear_controls',False):
        for key in [prefix+'-request',*[prefix+'-'+k for k in saved]]:st.session_state.pop(key,None)
    with st.expander('本轮讲解要求'):
        request=st.text_area('这次想弄懂什么（可选）',key=prefix+'-request',max_chars=1200,
                             placeholder='例如：为什么要先通分？这次请详细一点。',disabled=demo_enabled())
        overrides={}
        for k in saved:
            choice=st.selectbox('这轮的'+LABELS[k],['inherit',*SETTING_OPTIONS[k]],format_func=VALUES.get,
                                key=prefix+'-'+k,disabled=demo_enabled())
            if choice!='inherit':overrides[k]=choice
        st.caption('只用于下一次成功的分析或追问，随后恢复长期设置；换题也会清除。失败时保留输入，方便检查。')
    return {'task':task,'settings':saved,'request':request,'overrides':overrides,'prefix':prefix}


def learning(options,intent='analyze'):
    return context.learning(options['task'],options['settings'],request=options['request'],
                            intent=intent,overrides=options['overrides'])


def analysis_key(options,base,intent='analyze'):
    task=options['task']
    if (not options['request'] and not options['overrides'] and task.get('last_analysis_base')==base
            and task.get('last_analysis_settings')==options['settings']):return task['last_analysis_key']
    return base+'-'+digest(learning(options,intent))


def analyzed(options,base,key,result,origin):
    task=options['task'];task['last_analysis_base']=base;task['last_analysis_key']=key
    task['last_analysis_settings']=deepcopy(options['settings'])
    analysis=result.get('analysis',result)
    explanation=analysis['summary']+'\n'+'\n'.join(f'{i}. {s}' for i,s in enumerate(analysis['steps'],1))
    context.remember(task,digest(key),options['request'] or '分析这道题',explanation,origin+'（未核对模型说明）')
    task['clear_controls']=True


def seed_analysis(options,active,result):
    if options['task']['turns'] or not context.fresh(active.get('created_at')):return
    analysis=result.get('analysis',result)
    explanation=analysis['summary']+'\n'+'\n'.join(f'{i}. {s}' for i,s in enumerate(analysis['steps'],1))
    context.remember(options['task'],digest(active['request_key']),'最近的同题分析',explanation,
                     active['origin']+'（未核对模型说明）')
    # 保留原生成时间；再次打开并不延长参考的有效期。
    for turn in options['task']['turns']:
        if turn['request_id']==digest(active['request_key']):turn['at']=active['created_at']


def render_coach(notebook,options,question,level,work,image,*,kind,work_image=None,confirmed=False,previous_entry=None):
    from study import agent_ui
    from study.ui import run_once,reply_origin
    task=options['task']
    st.markdown('**继续问这道题**')
    st.caption('可先在“本轮讲解要求”写问题。提示和追问不改原作答，也不自动收藏。')
    columns=st.columns(3 if previous_entry else 2)
    intent=None
    for col,label,choice in zip(columns,['只给一个提示','继续讲解','比较本次订正'],['hint','explain','compare']):
        if col.button(label,key=options['prefix']+'-'+choice,disabled=not confirmed or demo_enabled()):intent=choice
    if intent:
        value=learning(options,intent)
        # 同一问题、同一上下文、同一意图点击不会重复发送。新输入/下一意图形成新轮。
        if (task.get('coach_request')==[options['request'],options['overrides'],intent,options['settings'],task['selected_next_step']]
                and task.get('coach_origin')==reply_origin() and task.get('coach_after_dialogue')==digest(task['turns'])):
            key=task['coach_key']
        else:key='coach-'+digest([task['identity'],value,reply_origin()])
        try:
            with st.spinner('正在结合本题回答…'):
                packet=run_once(key,lambda:agent_ui.perform(notebook,key,'coach',question,level,work,image,
                    work_kind=kind,work_image=work_image,learning=value,previous_entry=previous_entry),cache_name='study_coach_calls')
            task['coach_key']=key
            task['coach_request']=[options['request'],deepcopy(options['overrides']),intent,deepcopy(options['settings']),task['selected_next_step']]
            task['coach']=packet['value']
            context.remember(task,digest(key),options['request'] or {'hint':'给一个提示','explain':'继续讲解','compare':'比较订正'}[intent],
                             packet['value']['reply'] or packet['value']['clarification'],reply_origin()+'（未核对模型说明）')
            task['clear_controls']=True
            # 清空临时输入后仍将重复点击视为本次请求。
            task['coach_request']=['',{},intent,deepcopy(options['settings']),task['selected_next_step']]
            task['coach_origin']=reply_origin();task['coach_after_dialogue']=digest(task['turns'])
            st.rerun()
        except (ValueError,OSError) as exc:st.error(str(exc))
    if task.get('coach'):
        result=task['coach']
        st.write(result['reply'] or result['clarification'])
        st.caption('要继续讨论，请补充本轮问题或选择下面的下一步；重复点击同一请求不会再发送。')
        if result['next_step']:
            st.info('建议下一步：'+result['next_step'])
            if st.button('把这一步作为接下来的任务',key=options['prefix']+'-next'):
                context.select_next(task,result);st.rerun()
        agent_ui.show_trace(notebook,task['coach_key'])
    if task['turns']:
        with st.expander('本题近期对话（当前会话）'):
            for turn in task['turns']:
                st.caption(turn['user_request']);st.write(turn['model_reply'])
            st.caption('最多保留最近四轮、24 小时内的内容；切换题目、资料权限或来源变化后清空。回复未经教师核对。')


def history_controls(notebook,entry):
    store=LocalPreferences(notebook.directory.parent,history=True);saved=store.load()
    current=saved['records'].get(entry['id'],{'enabled':True,'include_model':True})
    with st.expander('这道题作为历史资料'):
        with st.form('history-controls-'+entry['id']+'-'+str(saved['version'])):
            allowed=st.checkbox('允许其他题目查询这条历史',value=current['enabled'])
            model=st.checkbox('允许引用这条历史的旧模型分析',value=current['include_model'])
            st.caption('旧分析可能有误，可单独停用；在“订正笔记”写下你的修正，来源中会区分标注。')
            if st.form_submit_button('确认历史使用范围'):
                try:
                    store.save(saved['version'],{**saved['records'],entry['id']:{'enabled':allowed,'include_model':model}})
                    st.rerun()
                except (ValueError,OSError) as exc:st.error(str(exc))
        st.caption('全局历史开关仍优先；回收站和超过 180 天未更新的历史不参与查询。停用不删除原记录。')
