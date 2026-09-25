"""两张合成图片的识图验证：冻结输入、逐次记账、失败即停；不自动分析或收藏。"""
import argparse
from dataclasses import asdict, replace
import getpass
import hashlib
import json
from pathlib import Path
import sys

from legacy.model_api import ModelConfig
from study.images import prepare_image
from study.output_contract import endpoint
from study.run_audit import AuditError, RunAudit, read_json, write_json
from study.service import OCR_PROMPT, StudyService, PhotoTransport, build_payload
from study.smoke import ROOT, default_config, source_snapshot
from study.transcription import observe, validate_observation

CASE_IDS=('p01_fraction_steps','q10_teacher_annotation')
SUITE='photo-ocr-smoke-v1'


def inputs(config):
    directory=ROOT/'evaluation/photo_cases_v1'
    cases={c['id']:c for c in read_json(directory/'manifest.json')['cases']}
    rows=[]
    for index,case_id in enumerate(CASE_IDS,1):
        case=cases[case_id]
        path=(directory/case['image']).resolve()
        if not path.is_relative_to(directory.resolve()): raise AuditError('图片必须位于合成测试集内。')
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=case['sha256']: raise AuditError('测试图片已改变。')
        photo=prepare_image(raw)
        rows.append({'row_id':f'b{index:02}','case_id':case_id,'operation':'recognize','depends_on':None,
            'image':photo,'original_sha256':case['sha256'],
            'payload':build_payload(config,OCR_PROMPT,{'task':'请分开转录题目与学生原作答。','provided_question':''},
                                    photo,operation='recognize')})
    return rows


def create_run(directory, *, mode='real_api'):
    if mode not in ('real_api','local_http_test'): raise AuditError('未知运行模式。')
    config=replace(default_config(),max_output_tokens=1024)
    audit=RunAudit.create(directory,{'schema_version':1,'suite':SUITE,'execution_mode':mode,
        'code':source_snapshot(),'config':asdict(config),'provider_endpoint':endpoint(config,'json_object'),
        'output_mode':'json_object','image_kind':'synthetic_not_handwriting','rows':inputs(config)})
    cases={c['id']:c for c in read_json(ROOT/'evaluation/photo_cases_v1/manifest.json')['cases']}
    write_json(audit.directory/'review.json',{'label':'人工核对参考；不进入请求；null 为未评',
        'rows':[{'row_id':r['row_id'],'case_id':r['case_id'],'reference_question':cases[r['case_id']]['question'],
                 'reference_student_work':cases[r['case_id']]['student_work'],
                 'checks':cases[r['case_id']]['checks'],'ocr_score_0_1_2':None,'separation_score_0_1_2':None,
                 'notes':''} for r in audit.rows.values()]})
    return audit


def verify(audit, *, live=False):
    current=source_snapshot()
    if audit.manifest.get('suite')!=SUITE or audit.manifest['code']!=current:
        raise AuditError('计划类型或冻结代码已改变，未发请求。')
    config=replace(default_config(),max_output_tokens=1024)
    if (audit.manifest['config']!=asdict(config) or list(audit.rows.values())!=inputs(config)
            or audit.manifest['provider_endpoint']!=endpoint(config,'json_object')
            or audit.manifest.get('output_mode')!='json_object'):
        raise AuditError('配置、图片或请求与冻结计划不同。')
    if live and (not current['commit'] or current['dirty']):
        raise AuditError('真实请求前需要干净的 Git 提交。')
    for row_id,planned in audit.rows.items():
        row=audit.row(row_id)
        if row and row['status']=='reply_valid':
            path=audit.directory/(row_id+'-observation.json')
            if not path.is_file(): raise AuditError('成功回复缺少识图原始记录，先检查报告，不续发。')
            observation=validate_observation(read_json(path))
            if (observation['raw']!=row['result'] or observation['images']!={'question':planned['image']['sha256'],'student_work':None}
                    or observation['mode']!=audit.manifest['execution_mode']):
                raise AuditError('识图原始记录与实际回复或图片不一致。')


def preflight(audit, confirm_plan, max_requests, budget_note):
    if audit.manifest['execution_mode']!='real_api': raise AuditError('本机演练不能升级为真实请求。')
    verify(audit,live=True)
    if (confirm_plan!=audit.manifest['plan_id'] or type(max_requests) is not int
            or max_requests!=len(audit.rows) or type(budget_note) is not str
            or not budget_note.strip() or len(budget_note)>500):
        raise AuditError('请确认完整计划编号、两次请求上限与预算。')
    approval={'plan_id':confirm_plan,'max_requests':max_requests,'budget_note':budget_note}
    path=audit.directory/'approval.json'
    if path.exists() and read_json(path)!=approval: raise AuditError('不能更换已确认预算。')
    state=audit.status()
    if state['counts']['failed'] or state['counts']['running']:
        raise AuditError('已有失败或完成情况未知的请求，不能续发或重试。')
    return approval


def execute(audit, *, key='', confirm_plan=None, max_requests=None, budget_note=None, server=None):
    live=audit.manifest['execution_mode']=='real_api'
    verify(audit,live=live)
    if live:
        if server is not None: raise AuditError('真实计划不能注入本机响应。')
        approval=preflight(audit,confirm_plan,max_requests,budget_note)
        StudyService(ModelConfig(**audit.manifest['config']),key)
        if key in budget_note: raise AuditError('预算中不能包含密钥。')
    elif audit.manifest['execution_mode']!='local_http_test' or server is None:
        raise AuditError('本机演练需要本机 HTTP 服务。')
    with audit.locked():
        state=audit.status()
        if state['counts']['failed'] or state['counts']['running']: raise AuditError('已有失败或未知完成记录，已停止。')
        if live:
            preflight(audit,confirm_plan,max_requests,budget_note)
            write_json(audit.directory/'approval.json',approval)
        for row_id,row in audit.rows.items():
            if audit.row(row_id) is not None: continue
            audit.expected_payload=row['payload']
            service=StudyService(ModelConfig(**audit.manifest['config']),key if live else 'local-test-key',
                None if live else PhotoTransport(server.chat_url),audit=audit,request_id=row_id)
            try:
                result=service.recognize(row['image'])
                observation=observe(result,row['image'],mode=audit.manifest['execution_mode'],
                                    origin='DeepSeek / '+audit.manifest['config']['model'] if live else '本机 HTTP 手写响应')
                write_json(audit.directory/(row_id+'-observation.json'),observation)
            except (ValueError,OSError):
                state=audit.status();state['execution_error']='stopped; inspect report; do not retry'
                write_json(audit.directory/'status.json',state)
                return state
        state=audit.status();write_json(audit.directory/'status.json',state)
    return state


def local(directory):
    from legacy.http_test_support import LocalModelServer
    audit=create_run(directory,mode='local_http_test')
    references={c['id']:c for c in read_json(ROOT/'evaluation/photo_cases_v1/manifest.json')['cases']}
    def reply(payload):
        case=references[next(r['case_id'] for r in audit.rows.values() if r['payload']==payload)]
        value={'text':case['question'],'student_work':case['student_work'],'work_kind':case['work_kind'],'warnings':[]}
        return {'object':'chat.completion','model':'local-handwritten-fixture',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(value,ensure_ascii=False)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}}
    with LocalModelServer() as server:
        server.body=reply
        return execute(audit,server=server)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('preview','local','status','check','run'))
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--confirm-plan')
    parser.add_argument('--max-requests',type=int)
    parser.add_argument('--budget-note')
    args=parser.parse_args(argv)
    try:
        if args.action=='preview': state=create_run(args.directory).status()
        elif args.action=='local': state=local(args.directory)
        else:
            audit=RunAudit(args.directory)
            if args.action=='status': state=audit.status()
            elif args.action=='check':
                verify(audit,live=audit.manifest['execution_mode']=='real_api');state=audit.status()
            else:
                preflight(audit,args.confirm_plan,args.max_requests,args.budget_note)
                if audit.status()['counts']['pending']==0: state=audit.status()
                else:
                    if not sys.stdin.isatty(): raise AuditError('请在本机终端隐藏输入密钥，不从聊天或参数读取。')
                    key=getpass.getpass('DeepSeek API 密钥（仅本次进程，隐藏输入）：')
                    state=execute(audit,key=key,confirm_plan=args.confirm_plan,max_requests=args.max_requests,budget_note=args.budget_note)
        print(json.dumps(state,ensure_ascii=False))
        return 2 if state.get('execution_error') or state['counts']['failed'] or state['counts']['running'] else 0
    except AuditError as exc:
        print(str(exc),file=sys.stderr);return 2
    except (ValueError,OSError,KeyError,TypeError):
        print('未执行或已停止，请检查报告；没有自动重试。',file=sys.stderr);return 2


if __name__=='__main__': raise SystemExit(main())
