"""默认零请求预览；真实 Agent 联调需冻结计划编号、次数、预算与本机隐藏输入。"""
import argparse
import getpass
import json
from pathlib import Path
import sys

from study.agent_live import AgentPlan, create, verify, authorize, execute, report, decide, MAX_REQUESTS
from study.run_audit import AuditError, read_json


def local_response(plan):
    # 验证的是同一执行器的协议边界，工具选择与最终内容均为手写响应。
    from tools.demo_study_agent import envelope
    replies=read_json(Path(__file__).resolve().parents[1]/'evaluation/study_smoke_local_responses.json')['responses']
    def response(payload):
        context=json.loads(payload['messages'][1]['content'][0]['text'])
        matches=[r for r in plan.rows.values() if json.loads(r['first_payload']['messages'][1]['content'][0]['text'])==context]
        row=matches[0] if matches else None
        if row is None:raise AuditError('找不到手写响应，不伪造模型结果。')
        # b02 和 b05 的用户输入相同，依靠已登记的当前场景区分演练故障。
        row=next((r for r in matches if plan.row(r['row_id'])
                  and plan.row(r['row_id'])['status']=='running'),row)
        scenario=row['scenario']
        if len(payload['messages'])==2 and scenario!='direct':
            name='get_review_history' if scenario=='history' else 'search_notes'
            arguments={'topic':'分数','limit':3} if scenario=='history' else {'query':'通分'}
            return envelope(calls=[('call_1',name,arguments)])
        sources=[] if len(payload['messages'])==2 else json.loads(payload['messages'][-1]['content'])['sources']
        citations=[{'source_id':r['source_id'],'quote':r['snippet'][:60]} for r in sources[:1]]
        return envelope(result=replies[row['local_fixture']],citations=citations)
    return response


def local(output):
    from http_test_support import LocalModelServer
    plan=create(output,'local_http_test')
    with LocalModelServer() as server:
        server.body=local_response(plan)
        state=execute(plan,server=server)
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='preview',choices=('preview','local','status','decide','run'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--directory',type=Path)
    parser.add_argument('--confirm-plan')
    parser.add_argument('--max-model-requests',type=int)
    parser.add_argument('--budget-cny',type=float)
    parser.add_argument('--row')
    choice=parser.add_mutually_exclusive_group()
    choice.add_argument('--accept',action='store_true')
    choice.add_argument('--reject',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.action!='run' and any(v is not None for v in (args.confirm_plan,args.max_model_requests,args.budget_cny)):
            raise AuditError('确认参数只能用于 run。')
        if args.action!='decide' and (args.row or args.accept or args.reject):raise AuditError('保存决定只能用于 decide。')
        if args.action in ('preview','local'):
            if args.output is None or args.directory is not None:raise AuditError('请提供新的 --output 目录。')
            result=report(create(args.output)) if args.action=='preview' else local(args.output)
        else:
            if args.directory is None or args.output is not None:raise AuditError('请提供已有 --directory。')
            plan=AgentPlan(args.directory)
            if args.action=='status':result=plan.status()
            elif args.action=='decide':
                if not args.row or not(args.accept or args.reject):raise AuditError('需指定 --row 和一个决定。')
                result=decide(plan,args.row,'accept' if args.accept else 'reject')
            else:
                if plan.manifest['execution_mode']!='real_api':raise AuditError('本机响应计划不能升级成真实计划。')
                verify(plan,live=True);authorize(plan,args.confirm_plan,args.max_model_requests,args.budget_cny)
                state=plan.status()
                if state['counts']['failed'] or state['counts']['running']:raise AuditError('已有失败或未完成场景；请回 Codex 检查，不能重试。')
                if state['counts']['pending']==0:
                    print(json.dumps(state,ensure_ascii=False));return 0
                if not sys.stdin.isatty():raise AuditError('真实密钥仅在本机交互终端隐藏输入，不读取聊天、参数或旧环境密钥。')
                print(f'冻结计划核对通过：5 个场景，累计最多 {MAX_REQUESTS} 次模型请求，预留 1 元（非供应商金额封顶）。')
                print('模型自主选择工具；第五项含本机故障注入。任何失败即停止，不重试、不自动收藏。')
                key=getpass.getpass('DeepSeek API 密钥（隐藏输入，仅本次进程使用）：')
                result=execute(plan,key=key,confirm_plan=args.confirm_plan,max_requests=args.max_model_requests,budget_cny=args.budget_cny)
        print(json.dumps(result,ensure_ascii=False))
        if args.action=='local':
            expected={'b01':'direct_observed','b02':'notes_observed','b03':'no_results_observed',
                      'b04':'history_observed','b05':'injected_tool_failure_observed'}
            return 0 if result['observed_branches']==expected and result['reserved_model_requests']==8 else 2
        if args.action=='run':print('请回到 Codex 检查工具调用和回复原文；失败不重试、不另建计划。')
        return 2 if args.action=='run' and (result['counts']['failed'] or result['counts']['running']) else 0
    except (KeyboardInterrupt,EOFError):
        print('已中断；已有调用状态以运行记录为准，不会自动重发。',file=sys.stderr)
        return 130
    except (ValueError,OSError,KeyError,TypeError):
        print('未执行或已停止：请检查冻结计划、预算与已有状态；不自动重试。',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
