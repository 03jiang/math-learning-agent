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
        st.session_state.photo_observation=None
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
    return StudyService(config,key,output_mode=os.environ.get('MATH_STUDY_OUTPUT_MODE','json_object'))


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
    st.markdown('**参考解法 · 可核对的思路与步骤**')
    st.write(analysis['summary'])
    for index, step in enumerate(analysis['steps'],1): st.write(f'{index}. {step}')
    if analysis['answer']:
        with st.expander('查看参考答案'):
            st.write(analysis['answer'])
    if analysis.get('error_analysis'):
        st.markdown('**对照你的作答**')
        st.write(analysis['error_analysis'])
        st.caption('这是旧版分析，没有结构化步骤证据。')
    if analysis.get('schema_version')==2:
        review=analysis['student_review']
        st.markdown('**你的作答与参考解法**')
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
                label={'correct':'这一步可成立','incorrect':'这一步需要订正','uncertain':'这一步还需核对'}[row['verdict']]
                st.markdown(f'**对照 {index+1} · {label}**')
                if index==first_error: st.caption('本次对照中最早发现的问题，从这里开始订正。')
                student,reference=st.columns(2)
                with student:
                    st.caption('你的原作答')
                    st.text(row['student_excerpt'])
                with reference:
                    st.caption('对应的参考做法')
                    st.write(row['reference_step'])
                st.write(row['explanation'])
        st.markdown('**可能需要巩固的内容**')
        for item in analysis['diagnosis']:
            with st.container(border=True):
                st.write(f"{item['knowledge_point']} · {item['category']}（待核对）")
                st.text('作答证据：'+item['evidence'])
                st.write(item['explanation'])
                st.write('用这个问题核对：'+item['check_question'])
        if not analysis['diagnosis']:
            st.caption('本次没有可确认的具体错因假设。知识点表示本题涉及的内容，不等于你的薄弱项。')
        if analysis['takeaway']:
            st.markdown('**同类题怎么做**')
            st.write(analysis['takeaway'])
    if analysis['next_practice']:
        st.info('下一步自检：'+analysis['next_practice'])
    st.caption('以上是人工编写的展示示例。' if example else '以上是模型生成的学习建议，尚未经过教师核对。')


def show_transcription(observation, current, *, saved=False):
    with st.expander('对照原始识别与核对内容'):
        st.caption(observation['origin']+' · '+observation['observed_at'])
        if observation['mode']=='offline_demo': st.caption('离线预设文字，不代表真实识图效果。')
        left,right=st.columns(2)
        for column,title,value in ((left,'原始识别（保留不改）',observation['raw']),
                                   (right,'收藏时核对内容' if saved else '当前核对草稿',current)):
            with column:
                st.markdown('**'+title+'**')
                st.text('题目：'+value['text'])
                st.text('作答：'+(value['student_work'] or '未提供'))
                st.caption('作答类型：'+WORK_KINDS[value['work_kind']])
        labels={'text':'题目','student_work':'作答','work_kind':'作答类型'}
        changed=[labels[k] for k,v in current.items() if v!=observation['raw'][k]]
        st.caption('已修改：'+'、'.join(changed) if changed else '核对内容与原始识别一致。')
        for warning in observation['raw']['warnings']: st.warning(warning)
        if not saved: st.caption('点击“确认加入错题本”后，这两份内容才会一起保存。')


def render_capture(notebook):
    entry_id=draft()
    st.title('拍下题目，慢慢弄懂')
    st.caption('把题目和你的作答一起放进来。核对后看步骤对照，归纳知识点，再收进错题本。')
    if demo_enabled():
        from study.demo_service import load_draft
        st.info('离线完整演示 · 仅回放一道人工作答示例，零模型请求。收藏会写入独立演示目录。')
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
            st.success('已分别识别题目与作答 · 请逐项核对')
            for warning in recognized.get('warnings',[]): st.warning(warning)
            st.caption('请核对题目、原作答和作答类型。原作答保留当时的错误，不要先改成正确答案；教师批注应移出作答栏。')
        question=st.text_area('题目文字',key='photo_question',height=170,max_chars=6000,on_change=unconfirm_question,
                              placeholder='在这里修正识别结果，或输入任意一道数学题。')
        level=st.selectbox('学习阶段',LEVELS,key='photo_level')
        my_work=st.text_area('我的答案或解题过程（可选）',key='photo_work',height=140,max_chars=3000,
            on_change=unconfirm_question,placeholder='保留原来的答案，每行写一步。没有过程也可以，只是暂时不能判断具体错因。')
        work_kind='none'
        if my_work.strip():
            work_kind=st.radio('当前作答包含什么',['unclear','answer_only','steps'],
                format_func=WORK_KINDS.get,key='photo_work_kind',horizontal=True,on_change=unconfirm_question)
        observation=st.session_state.get('photo_observation')
        if observation:
            show_transcription(observation,{'text':question.strip(),'student_work':my_work.strip(),'work_kind':work_kind})
        st.caption('核对范围包括题干、图形、原作答及作答类型。修改任一作答内容后需要重新勾选。')
        confirmed=st.checkbox('题干与图形条件已核对',key='photo_confirmed')
        current=fingerprint(question,level,my_work,image,work_kind,work_image=work_image)
        from study import agent_ui
        current+=agent_ui.signature(notebook)
        key='analysis-v4-'+entry_id+'-'+current+'-'+st.session_state.get('photo_model','')
        if st.button('分析这道题',type='primary',disabled=not(question.strip() and confirmed)):
            try:
                with st.spinner('正在分析思路与解题步骤…'):
                    packet=run_once(key,lambda:agent_ui.perform(notebook,key,'analyze',question,level,my_work,image,
                                                              work_kind=work_kind,work_image=work_image))
                st.session_state.photo_analysis={'fingerprint':current,'value':packet['value'],
                                                'agent_run':packet['agent_run'],'origin':reply_origin()}
            except (ValueError,OSError) as exc: st.error(str(exc))
        agent_ui.show_trace(notebook,key)
        st.caption('当前只回放人工编写的固定示例，不发送图片或文字。' if demo_enabled() else
                   '分析会发送本题文字、作答和附图给 DeepSeek。未配置密钥时，可先手动整理并收藏。')
        if not demo_enabled() and not(st.session_state.get('photo_api_key') or os.environ.get('DEEPSEEK_API_KEY')):
            st.info('尚未连接 DeepSeek。需要识图或分析时，展开左侧“连接 DeepSeek”；现在也可以先手动收藏。')
        stored=st.session_state.photo_analysis
        active=stored if stored and stored['fingerprint']==current else None
        if stored and not active: st.info('题目或作答已修改，原分析不再用于本次收藏，请重新分析。')
        if active:
            st.success('已分析 · 可以对照步骤整理错因')
            show_analysis(active['value'],example=active['origin']==DEMO_ORIGIN)
            st.button('放弃这次分析',on_click=agent_ui.discard_candidate,args=(notebook,active,key))
        if st.session_state.get('study_agent_action_error'):
            st.error(st.session_state.pop('study_agent_action_error'))
        failed=[key for key,value in st.session_state.photo_calls.items() if 'error' in value]
        if failed and st.button('清除失败记录，允许重新点击请求'):
            for key in failed: del st.session_state.photo_calls[key]
            st.rerun()
    st.divider()
    st.subheader('3 · 留下一条自己的总结')
    st.caption('不用等到完全弄懂再收藏。模型的错因是假设；你核对后再选择，“尚不确定”也可以。')
    # 分析完成或输入变化后刷新表单默认分类，避免保留分析前的“待整理”。
    form_revision=current+('-analyzed' if active else '-manual')
    with st.form('photo-save-'+entry_id+'-'+form_revision):
        topic=st.text_input('知识点 / 分类',value=active['value']['topic'] if active else '待整理',max_chars=80)
        reason=st.selectbox('我想复习的原因',REASONS)
        correction=st.text_area('我的订正与提醒',placeholder='例如：先圈出题目问什么，再列式。',max_chars=3000)
        saved=st.form_submit_button('确认加入错题本',type='primary',disabled=not(question.strip() and confirmed))
    if saved:
        try:
            if active: agent_ui.before_save(notebook,active,active['value'])
            from study.transcription import confirm
            transcription=confirm(observation,question,my_work,work_kind,image,work_image) if observation else None
            entry=make_entry(entry_id,question=question,level=level,my_work=my_work,topic=topic,reason=reason,
                correction=correction,image=image,work_image=work_image,analysis=active['value'] if active else None,
                analysis_origin=active['origin'] if active else '手动整理（没有模型分析）',transcription=transcription)
            notebook.save_new(entry)
            if active: agent_ui.after_save(notebook,active,notebook.get(entry_id))
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
        st.info('还没有收藏。去“拍照解题”加入第一道值得回顾的题。')
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
    if entry.get('transcription'):
        show_transcription(entry['transcription']['observation'],entry['transcription']['confirmed'],saved=True)
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
    st.caption('“独立做对”是本次自评，不自动判断已经掌握。')
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
    st.caption('回收站保留原题、两张照片、分析、订正和自评；这里的题不计入学习回顾。')
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
    st.caption('根据你确认的错因与复习记录整理，不推断能力等级。')
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
    st.caption('这里按未复习或仍需练习的题目，优先展示等待较久的记录。没有调用模型。')
    report=['# 数学学习回顾',f"收藏 {summary['total']} 题，复习 {summary['review_count']} 次；还需练习 {summary['needs_practice']} 题。",
            '复习结果来自自评，不代表已掌握。','\n## 知识点',*[f'- {k}：{v} 题' for k,v in summary['topics'].items()],
            '\n## 已记录错因',*[f'- {k}：{v} 题' for k,v in summary['reasons'].items()]]
    st.subheader('把同类题放在一起总结')
    st.caption('按你收藏时确认的分类归纳。模型的方法建议保留来源，具体错因只统计你选择的结果。')
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
    if st.session_state.get('study_agent_save_notice'):
        st.warning(st.session_state.pop('study_agent_save_notice'))
    with st.sidebar:
        st.title('🌱 数学学习工作台')
        st.caption('小学 · 初中 · 高中\n\n自己的题目，自己的复习记录。')
        st.metric('已收藏',len(entries))
        with st.expander('连接 DeepSeek',expanded=False):
            offline=os.environ.get('MATH_PHOTO_OFFLINE')=='1' or demo_enabled()
            st.text_input('DeepSeek API 密钥',type='password',key='photo_api_key',disabled=offline)
            st.text_input('模型名称',value='deepseek-flash',key='photo_model',disabled=offline)
            st.caption('密钥只用于当前会话，不写入文件。点击识图或分析时才请求，可能产生费用。')
            if offline: st.info('当前为离线演示入口，DeepSeek 请求已关闭。')
            elif os.environ.get('DEEPSEEK_API_KEY'): st.caption('已从启动环境载入密钥。')
            st.button('清除会话密钥',on_click=clear_key)
        with st.expander('辅助资料（实验）'):
            st.checkbox('让模型按需查资料',key='study_agent_enabled',disabled=offline)
            st.checkbox('允许查询已收藏错题',key='study_agent_history',
                        disabled=offline or not st.session_state.get('study_agent_enabled'))
            st.caption('默认关闭；仅本次会话生效。启用后，每次分析或订正最多 4 次模型请求、3 次只读工具请求，可能增加费用。')
            st.caption('历史关闭时不发送其他错题。执行记录保存在本机；收藏仍由你确认。')
        st.caption('照片只在确认收藏后写入本地错题本。摄像头需浏览器授权。')
    for error in errors: st.warning(error)
    if view=='拍照解题': render_capture(notebook)
    elif view=='错题本': render_notebook(notebook,entries)
    else: render_summary(entries)
