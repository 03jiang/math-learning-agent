"""只读复用两份完成的 OCR；输入确认、分析请求和收藏决定分别记录。"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

from study.images import image_bytes
from study.run_audit import AuditError, RunAudit, digest, read_json, write_json, stamp
from study.transcription import confirm, validate_observation, validate_trace
from study.service import analysis_context, build_payload, ANALYSIS_PROMPT


def make_rows(directory, mode, config, output_mode):
    from study.smoke import ROOT
    from study.photo_smoke import SUITE, CASE_IDS
    source=RunAudit(directory)
    if (source.manifest.get('suite')!=SUITE or source.manifest['execution_mode']!=mode
            or list(source.rows)!=['b01','b02']
            or tuple(r['case_id'] for r in source.rows.values())!=CASE_IDS):
        raise AuditError('需同模式、已完成的两张识图来源；本机结果不能冒充真实结果。')
    cases={c['id']:c for c in read_json(ROOT/'evaluation/photo_cases_v1/manifest.json')['cases']}
    rows=[];paths=[source.directory/'manifest.json']
    for row_id,planned in source.rows.items():
        row=source.row(row_id)
        if (planned['operation']!='recognize' or not row or row['status']!='reply_valid'
                or row['mode']!=mode or row['call']['kind']!=mode or row['call']['status']!='ok'
                or row['call']['http_status']!=200 or row['call']['attempted_requests']!=1
                or row['call']['completion_unknown'] or row['payload']!=planned['payload']
                or row['request_hash']!=digest(row['payload']) or row['result_hash']!=digest(row['result'])):
            raise AuditError('识图来源未完整成功，或请求与回复记录不一致。')
        photo=planned['image'];image_bytes(photo)
        blocks=row['payload']['messages'][1]['content']
        context=json.loads(blocks[0]['text'])
        if (len(blocks)!=2 or context.get('provided_question')!=''
                or context.get('attached_images')!=[{'position':1,'role':'question'}]
                or blocks[1]!={'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+photo['base64']}}):
            raise AuditError('来源图片与实际发送内容不一致。')
        observation_path=source.directory/(row_id+'-observation.json')
        observation=validate_observation(read_json(observation_path))
        if (observation['raw']!=row['result'] or observation['mode']!=mode
                or observation['images']!={'question':photo['sha256'],'student_work':None}):
            raise AuditError('原始识图记录与回复或图片不符。')
        raw=observation['raw'];case=cases[planned['case_id']]
        context=analysis_context(raw['text'],case['level'],raw['student_work'],work_kind=raw['work_kind'])
        rows.append({'row_id':row_id,'case_id':planned['case_id'],'title':planned['case_id']+'／识图后分析',
            'operation':'analyze','depends_on':None,'context':context,'image':photo,'observation':observation,
            'expected_behavior':case['checks'],'human_reference':{'answer':case['answer'],'status':case['status']},
            'payload':build_payload(config,ANALYSIS_PROMPT,context,photo,output_mode=output_mode)})
        paths.extend((source.path(row_id),observation_path))
    frozen={'directory':str(source.directory),'plan_id':source.manifest['plan_id'],
            'artifact_hashes':{p.relative_to(source.directory).as_posix():sha256(p.read_bytes()).hexdigest() for p in paths}}
    return rows,frozen


def input_confirmation(audit):
    """只检查核对决定；不写错题本、不把确认输入当作接受模型分析。"""
    if 'ocr_source' not in audit.manifest: raise AuditError('此计划不是识图后的分析。')
    path=audit.directory/'input-confirmation.json'
    if not path.is_file(): raise AuditError('先确认两份识图文字，再运行分析；未读取密钥或发送请求。')
    value=read_json(path)
    if (set(value)!={'plan_id','actor','at','traces','confirmation_id'}
            or value['plan_id']!=audit.manifest['plan_id']
            or value['actor'] not in ('user','local_fixture')
            or (audit.manifest['execution_mode']=='real_api' and value['actor']!='user')
            or value['confirmation_id']!=digest({k:v for k,v in value.items() if k!='confirmation_id'})
            or set(value['traces'])!=set(audit.rows)):
        raise AuditError('输入核对记录无效或已改变。')
    datetime.fromisoformat(value['at'])
    for row_id,planned in audit.rows.items():
        context=planned['context'];trace=value['traces'][row_id]
        validate_trace(trace,question=context['confirmed_question'],my_work=context['student_work'],image=planned['image'])
        if (trace['observation']!=planned['observation'] or trace['confirmed_at']!=value['at']
                or trace['confirmed']['work_kind']!=context['student_work_kind']):
            raise AuditError('核对内容与本次冻结输入不一致。')
    return value


def confirm_inputs(audit, *, actor='user'):
    from study.smoke import verify_frozen
    verify_frozen(audit)
    if 'ocr_source' not in audit.manifest or actor not in ('user','local_fixture'):
        raise AuditError('只能确认识图后的分析输入。')
    if actor=='local_fixture' and audit.manifest['execution_mode']!='local_http_test':
        raise AuditError('真实分析的输入必须由用户确认。')
    with audit.locked():
        path=audit.directory/'input-confirmation.json'
        if path.exists():
            value=input_confirmation(audit)
            if value['actor']!=actor: raise AuditError('不能改写输入确认来源。')
            return value
        if audit.status()['reserved_attempts']:
            raise AuditError('不能在分析请求后补造输入核对记录。')
        at=stamp();traces={}
        for row_id,planned in audit.rows.items():
            context=planned['context']
            trace=confirm(planned['observation'],context['confirmed_question'],context['student_work'],
                          context['student_work_kind'],planned['image'])
            trace['confirmed_at']=at
            traces[row_id]=trace
        value={'plan_id':audit.manifest['plan_id'],'actor':actor,'at':at,'traces':traces}
        value['confirmation_id']=digest(value)
        write_json(path,value)
    return input_confirmation(audit)


def entry_fields(audit,row_id):
    value=input_confirmation(audit)
    return {'image':deepcopy(audit.rows[row_id]['image']),
            'transcription':deepcopy(value['traces'][row_id])}
