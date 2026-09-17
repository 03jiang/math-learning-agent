"""零密钥 Agent 演练：本机 HTTP 手写工具请求，不代表真实模型决策。"""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from http_test_support import LocalModelServer
from study.agent import AgentStudyService
from study.agent_audit import AgentAudit, assert_candidate, record_decision
from study.agent_tools import ToolScope
from study.notebook import Notebook, make_entry
from study.service import PhotoTransport
from study.smoke import default_config

ROOT=Path(__file__).resolve().parents[1]
SCENARIOS=('direct','notes','no_results','history','failure','budget','clarification')


def envelope(*,calls=None,result=None,citations=None):
    message={'role':'assistant','content':None}
    if calls is not None:
        message['tool_calls']=[{'id':str(i),'type':'function','function':{'name':name,
            'arguments':json.dumps(arguments,ensure_ascii=False)}} for i,name,arguments in calls]
    else:message['content']=json.dumps({'result':result,'citations':citations or []},ensure_ascii=False)
    return {'object':'chat.completion','model':'handwritten-offline-fixture',
            'choices':[{'finish_reason':'tool_calls' if calls is not None else 'stop','message':message}]}


def run(output):
    output=Path(output)
    if output.exists() or output.is_symlink():raise ValueError('输出目录已存在；不会覆盖，请换一个新目录。')
    cases=json.loads((ROOT/'evaluation/study_smoke_v1.json').read_text())['cases']
    responses=json.loads((ROOT/'evaluation/study_smoke_local_responses.json').read_text())['responses']
    output.mkdir(parents=True);rows=[]
    for name in SCENARIOS:
        folder=output/name;book=Notebook(folder/'notebook');audit=AgentAudit(folder/'agent-runs')
        case=cases[4] if name=='clarification' else cases[1]
        answer=responses['b05' if name=='clarification' else 'b02']
        if name=='history':
            previous=make_entry(uuid4().hex,question='计算 1/2 + 1/3。',level='小学',my_work='2/5',topic='通分')
            book.save_new(previous)  # 演练用人工已收藏记录，隔离于用户存档。
        scope=ToolScope(book.directory,history_enabled=name=='history')
        with LocalModelServer() as server:
            def scripted(payload):
                if name in ('direct','clarification'):return envelope(result=answer)
                if len(server.requests)==1:
                    if name=='failure':return envelope(calls=[('x','delete_notebook',{})])
                    if name=='budget':return envelope(calls=[(i,'search_notes',{'query':str(i)}) for i in range(4)])
                    if name=='history':return envelope(calls=[('h','get_review_history',{'topic':'通分','limit':2})])
                    return envelope(calls=[('n','search_notes',{'query':'unfindable_zebra' if name=='no_results' else '通分'})])
                sources=json.loads(payload['messages'][-1]['content'])['sources']
                citations=[{'source_id':s['source_id'],'quote':s['snippet'][:80]} for s in sources[:1]]
                return envelope(result=answer,citations=citations)
            server.body=scripted
            tutor=AgentStudyService(default_config(),'local-test-key',scope=scope,audit_dir=audit.directory,
                                    transport=PhotoTransport(server.chat_url))
            try:result=tutor.analyze(case['question'],case['level'],case['student_work'],work_kind=case['work_kind'])
            except ValueError:
                if name not in ('failure','budget'):raise
            else:
                if name=='notes':
                    assert_candidate(audit,tutor.run_id,result,scope)
                    saved=make_entry(uuid4().hex,question=case['question'],level=case['level'],my_work=case['student_work'],
                        analysis=result,analysis_origin='人工 HTTP 演练，非真实模型')
                    book.save_new(saved)
                    record_decision(audit,tutor.run_id,'accept',saved_entry=saved)
                    before=book.path(saved['id']).read_bytes()
                    record_decision(audit,tutor.run_id,'accept',saved_entry=saved)
                    assert before==book.path(saved['id']).read_bytes()
                    assert Notebook(book.directory).get(saved['id'])['reviews']==[]
                elif name=='direct':record_decision(audit,tutor.run_id,'reject')
            trace=audit.read(tutor.run_id)
            rows.append({'scenario':name,'status':trace['status'],'local_http_requests':len(server.requests),
                'tool_requests_seen':trace['tool_requests_seen'],'sources':list(trace['sources']),
                'decision':trace['decision'],'error_code':trace['error_code']})
    summary={'mode':'local_http_test','label':'预编工具选择和手写回复；不代表模型成绩',
             'real_api_calls':0,'simulated_user_decisions':True,'rows':rows}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:print(json.dumps(run(args.output),ensure_ascii=False))
    except (ValueError,OSError) as exc:print(str(exc));return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
