"""分开保留原始识图与人工核对内容；只有收藏时才随错题原子写入。"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from study.diagnosis import object_fields, work_kind_for
from study.images import image_bytes
from study.notebook import text

FIELDS=('text','student_work','work_kind')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def validate_recognition(value, *, provided_question=None):
    object_fields(value,{'text','student_work','work_kind','warnings'})
    text(value['text'],'识别题目',6000,True)
    text(value['student_work'],'识别作答',3000)
    work_kind_for(value['student_work'],value['work_kind'])
    if provided_question is not None and value['text']!=provided_question:
        raise ValueError('仅识别作答照片时不能改写用户输入的题目。')
    if type(value['warnings']) is not list or len(value['warnings'])>12:
        raise ValueError('识图提示无效。')
    for warning in value['warnings']: text(warning,'识图提示',500,True)
    return value


def image_hashes(image, work_image):
    for photo in (image,work_image):
        if photo is not None: image_bytes(photo)
    return {'question':image['sha256'] if image else None,
            'student_work':work_image['sha256'] if work_image else None}


def observe(raw, image, work_image=None, *, provided_question='', mode, origin):
    hashes=image_hashes(image,work_image)
    if not any(hashes.values()): raise ValueError('识图记录必须有照片来源。')
    text(provided_question,'识图前输入题目',6000,image is None)
    value={'schema_version':1,'observed_at':stamp(),'mode':mode,'origin':origin,
           'images':hashes,'provided_question':provided_question,'raw':deepcopy(raw)}
    value['observation_id']=digest(value)
    return validate_observation(value)


def validate_observation(value):
    object_fields(value,{'schema_version','observed_at','mode','origin','images','provided_question','raw','observation_id'})
    if type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('识图记录版本无效。')
    if value['mode'] not in ('real_api','local_http_test','offline_demo'):
        raise ValueError('识图来源无效。')
    text(value['origin'],'识图来源说明',100,True)
    datetime.fromisoformat(value['observed_at'])
    object_fields(value['images'],{'question','student_work'})
    if not any(value['images'].values()): raise ValueError('缺少识图图片。')
    for h in value['images'].values():
        if h is not None and (type(h) is not str or not re.fullmatch('[a-f0-9]{64}',h)):
            raise ValueError('识图图片指纹无效。')
    text(value['provided_question'],'识图前输入题目',6000,value['images']['question'] is None)
    validate_recognition(value['raw'],provided_question=value['provided_question'] if value['images']['question'] is None else None)
    if value['observation_id']!=digest({k:v for k,v in value.items() if k!='observation_id'}):
        raise ValueError('原始识图记录已变化，不能保存为原结果。')
    return value


def confirm(observation, question, my_work, work_kind, image, work_image=None):
    validate_observation(observation)
    current={'text':question.strip(),'student_work':my_work.strip(),'work_kind':work_kind}
    value={'schema_version':1,'observation':deepcopy(observation),'confirmed_at':stamp(),
           'confirmed':current,'changed_fields':[k for k in FIELDS if current[k]!=observation['raw'][k]]}
    return validate_trace(value,question=question.strip(),my_work=my_work.strip(),image=image,work_image=work_image)


def validate_trace(value, *, question, my_work, image, work_image=None):
    object_fields(value,{'schema_version','observation','confirmed_at','confirmed','changed_fields'})
    if type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('核对记录版本无效。')
    datetime.fromisoformat(value['confirmed_at'])
    original=validate_observation(value['observation'])
    if original['images']!=image_hashes(image,work_image):
        raise ValueError('照片已更换或调整，旧识图记录不能用于本次收藏。')
    current=value['confirmed']
    object_fields(current,{'text','student_work','work_kind'})
    text(current['text'],'核对后题目',6000,True)
    text(current['student_work'],'核对后作答',3000)
    work_kind_for(current['student_work'],current['work_kind'])
    if current['text']!=question or current['student_work']!=my_work:
        raise ValueError('核对记录与收藏内容不一致。')
    if value['changed_fields']!=[k for k in FIELDS if current[k]!=original['raw'][k]]:
        raise ValueError('人工修改记录不一致。')
    return value
