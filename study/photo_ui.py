"""两张照片分开录入；旋转、裁剪只改草稿，所有请求仍由按钮触发。"""
import hashlib
import json
from uuid import uuid4

import streamlit as st

from study.images import prepare_image, image_bytes, transform_image

SLOTS={'question':('photo_image','题目照片'),'work':('photo_work_image','作答照片')}


def invalidate():
    st.session_state.photo_confirmed=False
    st.session_state.photo_analysis=None
    st.session_state.photo_recognition=None
    st.session_state.photo_observation=None


def put_upload(role,raw):
    key,_=SLOTS[role]
    digest=hashlib.sha256(raw).hexdigest()
    if digest==st.session_state.get(key+'_upload_hash'): return
    if role=='question' and st.session_state.get(key) and digest==st.session_state.get('photo_uploaded_hash'):
        st.session_state[key+'_upload_hash']=digest
        st.session_state.setdefault(key+'_original',st.session_state[key])
        return
    normalized=prepare_image(raw)
    st.session_state[key]=normalized
    st.session_state[key+'_original']=normalized
    st.session_state[key+'_upload_hash']=digest
    invalidate()
    if role=='question':
        st.session_state.pop('photo_uploaded_hash',None)
        st.session_state.photo_question=''
        st.session_state.photo_work=''
        st.session_state.photo_work_kind='unclear'
        st.session_state.photo_draft_id=uuid4().hex
        for field in ('photo_work_image','photo_work_image_original','photo_work_image_upload_hash'):
            st.session_state[field]=None
        st.session_state.photo_work_upload_generation=uuid4().hex
    else:
        st.session_state.photo_work=''
        st.session_state.photo_work_kind='unclear'


def edit_photo(role):
    key,label=SLOTS[role]
    photo=st.session_state.get(key)
    if photo is None: return
    original=st.session_state.setdefault(key+'_original',photo) or photo
    with st.expander('调整'+label):
        st.caption('先旋转，再选择要保留的区域；预览满意后点击应用。')
        identity=key+'-'+original['sha256']
        rotation=st.selectbox(label+'旋转',[0,90,180,270],
            format_func=lambda angle:{0:'不旋转',90:'顺时针 90°',180:'旋转 180°',270:'逆时针 90°'}[angle],key=identity+'-rotation')
        horizontal=st.slider(label+'左右保留范围（%）',0,100,(0,100),key=identity+'-horizontal')
        vertical=st.slider(label+'上下保留范围（%）',0,100,(0,100),key=identity+'-vertical')
        preview=None
        try:
            preview=transform_image(original,rotation=rotation,horizontal=horizontal,vertical=vertical)
            st.image(image_bytes(preview),caption='调整预览 · 尚未应用',width='stretch')
        except ValueError as exc: st.warning(str(exc))
        if st.button('应用'+label+'调整',key=key+'-apply',disabled=preview is None):
            st.session_state[key]=preview
            invalidate()
            st.session_state.photo_image_notice='照片已调整，请重新核对题目和作答文字，必要时再次识图。'
            st.rerun()
        if st.button('恢复'+label+'原图',key=key+'-restore'):
            st.session_state[key]=original
            invalidate()
            st.session_state.photo_image_notice='已恢复本轮原图，请重新核对文字。'
            st.rerun()


def remove_photo(role):
    key,_=SLOTS[role]
    for field in (key,key+'_original',key+'_upload_hash'): st.session_state[field]=None
    if role=='question': st.session_state.pop('photo_uploaded_hash',None)
    invalidate()
    generation='photo_upload_generation' if role=='question' else 'photo_work_upload_generation'
    st.session_state[generation]=uuid4().hex


def render_inputs():
    from study.ui import service, run_once, reply_origin
    from study.transcription import observe
    st.subheader('1 · 放入题目与作答')
    source=st.radio('题目来源',['上传照片','使用摄像头','直接输入'],horizontal=True,key='photo_source')
    generation=st.session_state.get('photo_upload_generation','initial')
    upload=None
    if source=='上传照片':
        upload=st.file_uploader('选择题目照片',type=['jpg','jpeg','png','webp'],max_upload_size=8,key='upload-'+generation)
    elif source=='使用摄像头': upload=st.camera_input('拍一道题',key='camera-'+generation)
    if upload:
        try: put_upload('question',upload.getvalue())
        except ValueError as exc:
            st.error(str(exc))
            return False
    st.caption('题目和作答在同一张照片里，可以只传题目照片；分开拍时，再添加作答照片。每张不超过 8 MB。')
    work_generation=st.session_state.get('photo_work_upload_generation','initial')
    work=st.file_uploader('补充作答照片（可选）',type=['jpg','jpeg','png','webp'],max_upload_size=8,key='work-upload-'+work_generation)
    if work:
        try: put_upload('work',work.getvalue())
        except ValueError as exc:
            st.error(str(exc))
            return False
    for role,(key,label) in SLOTS.items():
        photo=st.session_state.get(key)
        if photo:
            st.image(image_bytes(photo),caption='本次'+label,width='stretch')
            edit_photo(role)
            st.button('移除'+label,key=key+'-remove',on_click=remove_photo,args=(role,))
    if st.session_state.get('photo_image_notice'):
        st.info(st.session_state.pop('photo_image_notice'))
    image=st.session_state.photo_image
    work_image=st.session_state.get('photo_work_image')
    question=st.session_state.get('photo_question','')
    if image or work_image:
        from study.demo_service import enabled as demo_enabled
        st.caption('离线演示只回放自带方程题的预设文字，不进行真实识图。' if demo_enabled() else
                   '点击识图会发送本轮已应用调整的照片；只有作答照片时，请先在右侧输入题目。')
        if st.button('识别这道题',type='primary',disabled=not(image or question.strip())):
            data=[image['sha256'] if image else None,work_image['sha256'] if work_image else None,
                  question if image is None else '',st.session_state.get('photo_model','')]
            key='ocr-v4-'+hashlib.sha256(json.dumps(data,ensure_ascii=False).encode()).hexdigest()
            def recognize_once():
                raw=service().recognize(image,work_image,question_text=question if image is None else '')
                return observe(raw,image,work_image,provided_question=question if image is None else '',
                    mode='offline_demo' if demo_enabled() else 'real_api',origin=reply_origin())
            try:
                with st.spinner('正在分别识别题目与学生作答…'):
                    observation=run_once(key,recognize_once)
                value=observation['raw']
                st.session_state.photo_observation=observation
                st.session_state.photo_recognition=value
                st.session_state.photo_question=value['text']
                st.session_state.photo_work=value['student_work']
                st.session_state.photo_work_kind=value['work_kind'] if value['work_kind']!='none' else 'unclear'
                st.session_state.photo_confirmed=False
                st.session_state.photo_analysis=None
            except (ValueError,OSError) as exc: st.error(str(exc))
    else: st.info('也可以直接在右侧输入题目和作答，从文字开始整理。')
    return True
