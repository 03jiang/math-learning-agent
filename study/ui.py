"""拍照 → 核对 → 分析 → 主动收藏 → 复习；上传与识别均不自动收藏。"""
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
from uuid import uuid4

import streamlit as st
from model_api import load_model_config
from study.images import image_bytes
from study.notebook import Notebook, LEVELS, REASONS, OUTCOMES, make_entry, summarize, learning_groups
from study.service import StudyService, fingerprint
from study.diagnosis import WORK_KINDS, VERDICTS
from study.demo_service import enabled as demo_enabled, ORIGIN as DEMO_ORIGIN

ROOT=Path(__file__).resolve().parents[1]


def reset_draft():
    for key in list(st.session_state):
        if key.startswith('photo_') and key not in ('photo_api_key','photo_model'):
            del st.session_state[key]
    st.session_state.photo_upload_generation=uuid4().hex


def unconfirm_question():
    st.session_state.photo_confirmed=False


def clear_key():
    st.session_state.pop('photo_api_key',None)


def draft():
    if 'photo_draft_id' not in st.session_state:
        st.session_state.photo_draft_id=uuid4().hex
        st.session_state.photo_question=''
        st.session_state.photo_work=''
        st.session_state.photo_work_kind='unclear'
        st.session_state.photo_image=None
        st.session_state.photo_work_image=None
        st.session_state.photo_recognition=None
        st.session_state.photo_analysis=None
        st.session_state.photo_calls={}
    return st.session_state.photo_draft_id


def service():
    if demo_enabled():
        from study.demo_service import DemoStudyService
        return DemoStudyService()
    if os.environ.get('MATH_PHOTO_OFFLINE')=='1':
        raise ValueError('此启动入口只运行本地功能。请使用“启动拍照错题本.command”连接 DeepSeek。')
    config=load_model_config(ROOT/'model_config.deepseek.example.json')
    config=replace(config,model=st.session_state.get('photo_model',config.model),max_output_tokens=4096,timeout_seconds=60)
    key=st.session_state.get('photo_api_key','') or os.environ.get('DEEPSEEK_API_KEY','')
    return StudyService(config,key)


def reply_origin():
    return DEMO_ORIGIN if demo_enabled() else 'DeepSeek / '+st.session_state.get('photo_model','deepseek-flash')


def run_once(key, function, *, cache_name='photo_calls'):
    calls=st.session_state.setdefault(cache_name,{})
    if key not in calls:
        calls[key]={'error':'上次请求未完成；不会自动重发。'}
        try:
            value=function()
            calls[key]={'value':value}
        except (ValueError,OSError) as exc:
            calls[key]={'error':str(exc)}
    result=calls[key]
    if 'error' in result: raise ValueError(result['error'])
    return result['value']


def show_analysis(analysis, *, example=False):
    if analysis['status']=='needs_clarification':
        st.warning(analysis['clarification'])
        st.caption('先补充条件，再重新分析。当前没有确定答案。')
    if analysis.get('schema_version')==2:
        st.markdown('**这道题在学什么**')
        st.write(' · '.join(analysis['knowledge_points']) or '先补充条件，再确定知识点。')
    st.markdown('**参考解题步骤**')
    st.write(analysis['summary'])
    for index, step in enumerate(analysis['steps'],1): st.write(f'{index}. {step}')
    if analysis['answer']:
        with st.expander('查看参考答案'):
            st.write(analysis['answer'])
    if analysis.get('error_analysis'):
        st.markdown('**对照你的作答**')
        st.write(analysis['error_analysis'])
        st.caption('这份旧分析没有逐步对照，请结合原作答核对。')
    if analysis.get('schema_version')==2:
        review=analysis['student_review']
        st.markdown('**逐步对照你的作答**')
        st.write(VERDICTS[review['verdict']])
        if review['work_kind']=='none':
            st.info('还没有提供作答。这里只讲解题目，不判断你的错因。')
        elif review['work_kind']=='answer_only':
            st.info('目前只有最终答案，可以对照结果，但不能判断你哪一步出错。请补充列式或计算过程。')
        elif review['work_kind']=='unclear':
            st.info('作答过程还不清楚，请补充清晰的步骤再分析错因。')
        if review['observed_approach']: st.write('从你写出的步骤可以看到：'+review['observed_approach'])
        if review['answer_feedback']: st.write(review['answer_feedback'])
        first_error=next((i for i,row in enumerate(review['comparisons']) if row['verdict']=='incorrect'),None)
        for index,row in enumerate(review['comparisons']):
            with st.container(border=True):
                label={'correct':'这一步是对的','incorrect':'这一步需要订正','uncertain':'这一步还需核对'}[row['verdict']]
                st.markdown(f'**对照 {index+1} · {label}**')
                if index==first_error: st.caption('先看这里：这是本次分析中最早发现的问题。')
                student,reference=st.columns(2)
                with student:
                    st.caption('你的原作答')
                    st.text(row['student_excerpt'])
                with reference:
                    st.caption('对应的参考做法')
                    st.write(row['reference_step'])
                st.write(row['explanation'])
        st.markdown('**这次可能错在哪里**')
        for item in analysis['diagnosis']:
            with st.container(border=True):
                st.write(f"{item['knowledge_point']} · {item['category']}（待核对）")
                st.text('你写的是：'+item['evidence'])
                st.write(item['explanation'])
                st.write('试着回答：'+item['check_question'])
        if not analysis['diagnosis']:
            st.caption('目前还不能确定具体错因。列出的知识点只是这道题涉及的内容，不代表你都不会。')
        if analysis['takeaway']:
            st.markdown('**同类题怎么做**')
            st.write(analysis['takeaway'])
    if analysis['next_practice']:
        st.info('再试一步：'+analysis['next_practice'])
    st.caption('以上是人工编写的展示示例。' if example else '以上是模型生成的学习建议，尚未经过教师核对。')


def render_capture(notebook):
    entry_id=draft()
    st.title('整理一道数学题')
    st.caption('上传题目和你的解题过程，先核对文字，再看分析。值得复习的内容可以存进错题本。')
    if demo_enabled():
        from study.demo_service import load_draft
        st.info('这是离线演示，使用一道人工作答示例，不调用模型。保存的内容单独存放，不影响普通错题本。')
        dirty=bool(st.session_state.photo_question or st.session_state.photo_work or st.session_state.photo_image
                   or st.session_state.get('photo_work_image') or st.session_state.get('photo_saved'))
        st.button('载入演示题',on_click=load_draft,disabled=dirty)
    with st.expander('先看一个错题分析示例（离线）'):
        from study.example import QUESTION, STUDENT_WORK, ANALYSIS
        st.info('这是人工编写的示例，不调用模型、不代表识图效果，也不会加入你的错题本。')
        st.write(QUESTION)
        st.text('示例原作答：\n'+STUDENT_WORK)
        show_analysis(ANALYSIS,example=True)
        from study.example import corrected_example
        from study.correction_ui import show_comparison
        corrected_work,corrected_result=corrected_example()
        st.markdown('**继续看：订正以后**')
        st.text(corrected_work)
        show_comparison(corrected_result,example=True)
    if st.session_state.get('photo_saved'):
        st.success('已收进错题本。你可以去“错题本”补充订正，或开始下一道。')
        st.button('开始下一道题',on_click=reset_draft,type='primary')
        return
    photo_col, question_col=st.columns([1,1.4],gap='large')
    with photo_col:
        from study.photo_ui import render_inputs
        if not render_inputs(): return
    entry_id=st.session_state.photo_draft_id
    image=st.session_state.photo_image
    work_image=st.session_state.get('photo_work_image')
    with question_col:
        st.subheader('2 · 核对后再分析')
        recognized=st.session_state.photo_recognition
        if recognized:
            st.success('文字已识别，请检查题目和作答有没有识别错。')
            for warning in recognized.get('warnings',[]): st.warning(warning)
            st.caption('请保留自己当时的答案，包括写错的地方，不要先改对。教师批注请移出作答栏，再检查下面选的作答类型。')
        question=st.text_area('题目文字',key='photo_question',height=170,max_chars=6000,on_change=unconfirm_question,
                              placeholder='修改识别错的文字，或直接输入题目。')
        level=st.selectbox('学习阶段',LEVELS,key='photo_level')
        my_work=st.text_area('我的答案或解题过程（可选）',key='photo_work',height=140,max_chars=3000,
            on_change=unconfirm_question,placeholder='按原样写下答案，每行一步。只有答案也可以，但暂时无法判断哪一步出错。')
        work_kind='none'
        if my_work.strip():
            work_kind=st.radio('当前作答包含什么',['unclear','answer_only','steps'],
                format_func=WORK_KINDS.get,key='photo_work_kind',horizontal=True,on_change=unconfirm_question)
        st.caption('请一起核对题目、图形条件、原作答和作答类型。改过内容后，需要重新勾选确认。')
        confirmed=st.checkbox('题干与图形条件已核对',key='photo_confirmed')
        current=fingerprint(question,level,my_work,image,work_kind,work_image=work_image)
        if st.button('分析这道题',type='primary',disabled=not(question.strip() and confirmed)):
            key='analysis-v3-'+current+'-'+st.session_state.get('photo_model','')
            try:
                with st.spinner('正在分析解题步骤…'):
                    result=run_once(key,lambda:service().analyze(question,level,my_work,image,work_kind=work_kind,work_image=work_image))
                st.session_state.photo_analysis={'fingerprint':current,'value':result,
                                                'origin':reply_origin()}
            except (ValueError,OSError) as exc: st.error(str(exc))
        st.caption('当前只回放人工编写的固定示例，不发送图片或文字。' if demo_enabled() else
                   '分析会发送本题文字、作答和附图给 DeepSeek。未配置密钥时，可先手动整理并收藏。')
        if not demo_enabled() and not(st.session_state.get('photo_api_key') or os.environ.get('DEEPSEEK_API_KEY')):
            st.info('尚未连接 DeepSeek。需要识图或分析时，展开左侧“连接 DeepSeek”；现在也可以先手动收藏。')
        stored=st.session_state.photo_analysis
        active=stored if stored and stored['fingerprint']==current else None
        if stored and not active: st.info('你改过题目或作答，之前的分析不再适用。请重新分析后再保存。')
        if active:
            st.success('分析完成，可以对照步骤检查。')
            show_analysis(active['value'],example=active['origin']==DEMO_ORIGIN)
        failed=[key for key,value in st.session_state.photo_calls.items() if 'error' in value]
        if failed and st.button('清除失败记录，允许重新点击请求'):
            for key in failed: del st.session_state.photo_calls[key]
            st.rerun()
    st.divider()
    st.subheader('3 · 记下这道题的提醒')
    st.caption('还没完全弄懂，也可以先收藏。可能的错因需要自己核对，不确定时选“尚不确定”。')
    # 分析完成或输入变化后刷新表单默认分类，避免保留分析前的“待整理”。
    form_revision=current+('-analyzed' if active else '-manual')
    with st.form('photo-save-'+entry_id+'-'+form_revision):
        topic=st.text_input('知识点 / 分类',value=active['value']['topic'] if active else '待整理',max_chars=80)
        reason=st.selectbox('我想复习的原因',REASONS)
        correction=st.text_area('我的订正与提醒',placeholder='例如：先圈出题目问什么，再列式。',max_chars=3000)
        saved=st.form_submit_button('确认加入错题本',type='primary',disabled=not(question.strip() and confirmed))
    if saved:
        try:
            entry=make_entry(entry_id,question=question,level=level,my_work=my_work,topic=topic,reason=reason,
                correction=correction,image=image,work_image=work_image,analysis=active['value'] if active else None,
                analysis_origin=active['origin'] if active else '手动整理（没有模型分析）')
            notebook.save_new(entry)
            st.session_state.photo_saved=True
            st.rerun()
        except (ValueError,OSError) as exc: st.error(f'没有保存：{exc}')


def render_notebook(notebook,entries):
    st.title('我的错题本')
    st.caption('题目、订正和复习记录放在一起。先自己做一遍，再展开参考解答。')
    collection=st.radio('查看哪些题目',['我的错题','回收站'],horizontal=True,key='notebook_collection')
    if collection=='回收站':
        render_trash(notebook)
        return
    if not entries:
        st.info('还没有收藏的题目。到“拍照解题”添加第一道题。')
        return
    cols=st.columns([2,1,1])
    query=cols[0].text_input('搜索题目或知识点')
    reason=cols[1].selectbox('按错因筛选',['全部',*REASONS])
    stage=cols[2].selectbox('按复习状态筛选',['全部','尚未复习',*OUTCOMES])
    filtered=[e for e in entries if query.casefold() in (e['question']+' '+e['topic']+' '+e['correction']).casefold()
        and (reason=='全部' or e['reason']==reason)
        and (stage=='全部' or (not e['reviews'] and stage=='尚未复习') or (e['reviews'] and e['reviews'][-1]['outcome']==stage))]
    st.caption(f'共 {len(filtered)} 道题；复习状态来自你的记录。')
    if not filtered:
        st.info('没有符合条件的题目，试试调整筛选。')
        return
    selected=st.selectbox('打开一道题',[e['id'] for e in filtered],format_func=lambda i:next(e['topic']+' · '+e['question'][:48] for e in filtered if e['id']==i))
    entry=next(e for e in filtered if e['id']==selected)
    st.subheader(entry['topic'])
    st.write(entry['question'])
    if entry['image']:
        with st.expander('查看题目照片'): st.image(image_bytes(entry['image']),width='stretch')
    if entry.get('work_image'):
        with st.expander('查看作答照片'): st.image(image_bytes(entry['work_image']),width='stretch')
    with st.expander('查看原作答与参考分析'):
        st.write(entry['my_work'] or '未记录原作答。')
        st.caption(entry['analysis_origin'])
        if entry['analysis']: show_analysis(entry['analysis'],example=entry['analysis_origin']==DEMO_ORIGIN)
        else: st.info('这道题暂时没有模型分析。')
    revision=f"{entry['id']}-{entry['version']}"
    from study.correction_ui import render as render_correction
    render_correction(notebook,entry)
    with st.expander('整理错因与订正',expanded=True):
        with st.form('edit-'+revision):
            topic=st.text_input('知识点',entry['topic'],max_chars=80)
            reason=st.selectbox('错因',REASONS,index=REASONS.index(entry['reason']))
            correction=st.text_area('订正笔记',entry['correction'],max_chars=3000)
            edit=st.form_submit_button('保存整理')
        if edit:
            try:
                notebook.update(entry['id'],entry['version'],uuid4().hex,edit={'topic':topic,'reason':reason,'correction':correction})
                st.rerun()
            except (ValueError,OSError) as exc: st.error(str(exc))
    with st.form('review-'+revision):
        st.subheader('再做一次')
        answer=st.text_area('本次答案或解题思路',max_chars=3000)
        outcome=st.radio('这次的情况',OUTCOMES,horizontal=True)
        note=st.text_input('给下一次的提醒',max_chars=3000)
        reviewed=st.form_submit_button('记录本次复习',type='primary')
    if reviewed:
        try:
            notebook.update(entry['id'],entry['version'],uuid4().hex,review={'outcome':outcome,'answer':answer,'note':note})
            st.rerun()
        except (ValueError,OSError) as exc: st.error(str(exc))
    for row in reversed(entry['reviews']):
        with st.expander(row['at'][:10]+' · '+row['outcome']):
            st.write(row['answer'] or '未写答案。')
            st.write(row['note'])
    st.caption('“独立做对”是你对这次复习的自评，不表示已经完全掌握。')
    st.download_button('导出这道题（JSON，含照片）',json.dumps(entry,ensure_ascii=False,indent=2),
                       file_name='数学错题-'+entry['id'][:8]+'.json',mime='application/json')
    with st.expander('收起这道题'):
        st.caption('移入回收站后不再参与学习回顾，照片、分析和订正记录都会保留，可以随时恢复。')
        if st.button('移入回收站',key='archive-'+revision):
            try:
                notebook.set_archived(entry['id'],entry['version'],uuid4().hex,archived=True)
                st.rerun()
            except (ValueError,OSError) as exc: st.error(str(exc))


def render_trash(notebook):
    entries,_=notebook.list(archived=True)
    st.caption('回收站中的题目不计入学习回顾。原题、照片、分析和订正记录都还在，可以恢复。')
    if not entries:
        st.info('回收站是空的。')
        return
    selected=st.selectbox('恢复哪道题',[e['id'] for e in entries],
        format_func=lambda key:next(e['topic']+' · '+e['question'][:48] for e in entries if e['id']==key))
    entry=next(e for e in entries if e['id']==selected)
    st.write(entry['question'])
    st.caption(f"保留 {len(entry.get('corrections',[]))} 次订正、{len(entry['reviews'])} 次自评记录。")
    if st.button('恢复这道题',type='primary'):
        try:
            notebook.set_archived(entry['id'],entry['version'],uuid4().hex,archived=False)
            st.rerun()
        except (ValueError,OSError) as exc: st.error(str(exc))


def render_summary(entries):
    st.title('学习回顾')
    st.caption('这里汇总你保存的错因和复习记录，不给你的数学能力打分。')
    summary=summarize(entries)
    for col,label,key in zip(st.columns(4),['收藏题目','还需练习','本次自评做对','复习次数'],
                             ['total','needs_practice','self_reported_correct','review_count']): col.metric(label,summary[key])
    if not entries:
        st.info('收藏第一道题后，这里会汇总知识点、常见错因和复习记录。')
        return
    left,right=st.columns(2)
    with left:
        st.subheader('按知识点整理')
        for name,count in sorted(summary['topics'].items(),key=lambda row:-row[1]): st.write(f'{name} · {count} 题')
    with right:
        st.subheader('我记录的错因')
        for name,count in sorted(summary['reasons'].items(),key=lambda row:-row[1]): st.write(f'{name} · {count} 题')
    st.subheader('接下来可以回顾')
    pending=sorted((e for e in entries if not e['reviews'] or e['reviews'][-1]['outcome']=='仍需练习'),
                   key=lambda e:e['reviews'][-1]['at'] if e['reviews'] else e['created_at'])
    if not pending: st.success('已收藏的题最近一次都记录为独立做对，可以隔一段时间再自测。')
    for entry in pending[:5]: st.write(f"**{entry['topic']}** · {entry['question'][:100]}")
    st.caption('优先列出较久没复习、或还需要练习的题目。这份列表由记录整理，没有调用模型。')
    report=['# 数学学习回顾',f"收藏 {summary['total']} 题，复习 {summary['review_count']} 次；还需练习 {summary['needs_practice']} 题。",
            '复习结果来自自评，不代表已掌握。','\n## 知识点',*[f'- {k}：{v} 题' for k,v in summary['topics'].items()],
            '\n## 已记录错因',*[f'- {k}：{v} 题' for k,v in summary['reasons'].items()]]
    st.subheader('把同类题放在一起总结')
    st.caption('按你保存的分类整理，错因只统计你选过的结果。模型建议会标出来源，供你核对。')
    report.append('\n## 同类题的方法与自检')
    for group in learning_groups(entries):
        title=f"{group['topic']} · {group['count']} 题 · {group['needs_practice']} 题还需练习"
        report.append('\n### '+title)
        with st.expander(title):
            for note in group['notes']:
                st.write(note['question'])
                report.append('题目：'+note['question'])
                if note['correction']:
                    st.write('我的提醒：'+note['correction'])
                    report.append('我的提醒：'+note['correction'])
                if note['correction_count']:
                    st.write(f"已保存 {note['correction_count']} 次订正")
                    st.caption('最近订正的分析总结，待核对 · '+note['analysis_origin'])
                    st.write(note['latest_change'])
                    report.append(f"已保存 {note['correction_count']} 次订正；最近模型总结（待核对）：{note['latest_change']}")
                if note['takeaway'] or note['next_practice']:
                    st.caption('分析建议，待核对 · '+note['analysis_origin'])
                    st.write(note['takeaway'])
                    st.write('自检：'+note['next_practice'])
                    report.extend(['模型建议（待核对，'+note['analysis_origin']+'）：'+note['takeaway'],
                                   '自检：'+note['next_practice']])
                st.divider()
    st.download_button('导出回顾摘要','\n\n'.join(report),file_name='数学学习回顾.md',mime='text/markdown')


def render(directory,view):
    notebook=Notebook(directory)
    entries,errors=notebook.list()
    with st.sidebar:
        st.title('数学错题助手')
        st.caption('整理题目，记录订正，留待下次复习。')
        st.metric('已收藏',len(entries))
        with st.expander('连接 DeepSeek',expanded=False):
            offline=os.environ.get('MATH_PHOTO_OFFLINE')=='1' or demo_enabled()
            st.text_input('DeepSeek API 密钥',type='password',key='photo_api_key',disabled=offline)
            st.text_input('模型名称',value='deepseek-flash',key='photo_model',disabled=offline)
            st.caption('密钥只用于当前会话，不写入文件。点击识图或分析时才请求，可能产生费用。')
            if offline: st.info('当前是离线演示，不会向 DeepSeek 发送请求。')
            elif os.environ.get('DEEPSEEK_API_KEY'): st.caption('已从启动环境载入密钥。')
            st.button('清除会话密钥',on_click=clear_key)
        st.caption('照片只在确认收藏后写入本地错题本。摄像头需浏览器授权。')
    for error in errors: st.warning(error)
    if view=='拍照解题': render_capture(notebook)
    elif view=='错题本': render_notebook(notebook,entries)
    else: render_summary(entries)
