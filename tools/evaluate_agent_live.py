"""A/B development preflight and audited execution; default preview makes zero HTTP calls."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from http_test_support import LocalModelServer
from study.evaluation import handwritten_response
from study.evaluation_live import DEFAULT_CASES, EvaluationPlan, create, execute, preflight
from study.evaluation_scoring import score_report
from study.run_audit import AuditError


def local_response(plan):
    def respond(payload):
        running = [r for row_id in plan.rows if (r := plan.row(row_id)) and r['status'] == 'running']
        if len(running) != 1:
            raise AuditError('本机脚本找不到唯一正在执行的回合。')
        row = running[0]
        case = plan.cases[row['case_id']]
        turn = case['turns'][row['turn_index']]
        return handwritten_response(row['input_context'], turn['operation'], case, row['arm'], payload)
    return respond


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='preview', choices=('preview', 'local', 'run', 'status', 'scores'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--cases', help='development IDs, comma separated; both arms and all case turns are included')
    parser.add_argument('--confirm-plan')
    parser.add_argument('--max-model-requests', type=int)
    parser.add_argument('--budget-cny')
    args = parser.parse_args(argv)
    try:
        if args.action != 'run' and (args.confirm_plan is not None or args.max_model_requests is not None):
            raise AuditError('执行确认参数只用于run。')
        if args.action in ('preview', 'local'):
            if args.output is None or args.directory is not None:
                raise AuditError('请提供新的 --output 目录。')
            plan = create(args.output, mode='real_api' if args.action == 'preview' else 'local_http_test',
                          case_ids=args.cases.split(',') if args.cases is not None else DEFAULT_CASES,
                          budget_cny=args.budget_cny or '2')
            if args.action == 'local':
                with LocalModelServer() as server:
                    server.body = local_response(plan)
                    result = execute(plan, server=server)
            else:
                result = plan.status()
        else:
            if args.directory is None or args.output is not None or args.cases is not None:
                raise AuditError('请提供已有 --directory；不能临时替换用例。')
            if args.action != 'run' and args.budget_cny is not None:
                raise AuditError('只读查询不接受预算更改。')
            plan = EvaluationPlan(args.directory)
            if args.action == 'status':
                result = plan.status()  # No writes, no key lookup, no request.
            elif args.action == 'scores':
                result = score_report(args.directory)
            else:
                state = preflight(plan, args.confirm_plan, args.max_model_requests, args.budget_cny)
                if state['execution_status'] == 'completed':
                    print(json.dumps(state, ensure_ascii=False))
                    return 0
                if not sys.stdin.isatty():
                    raise AuditError('密钥只在本机交互终端隐藏输入；不读取环境、文件或聊天密钥。')
                print(f"核对通过：{len(plan.rows)}回合，最多{state['max_model_requests']}次模型请求，预留{state['budget_cny']}元（估算，不是供应商封顶）。")
                print('失败即停，不自动续跑或收藏；两个方案使用同一模型和输出检查。')
                key = getpass.getpass('DeepSeek API 密钥（隐藏输入，仅本次进程使用）：')
                result = execute(plan, key=key, confirm_plan=args.confirm_plan,
                                 max_requests=args.max_model_requests, budget_cny=args.budget_cny)
                key = None
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('execution_status') in ('stopped', 'running') else 0
    except (KeyboardInterrupt, EOFError):
        print('已中断；请检查账本，不要重试或另建真实计划。', file=sys.stderr)
        return 130
    except AuditError as exc:
        print('已停止：' + str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print('记录、目录或输入无效，已停止；不重试、不覆盖。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
