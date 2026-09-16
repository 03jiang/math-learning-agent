"""当前主流程的文字验证：默认预览；local 为手写 HTTP；run 必须另行确认预算。"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_api import load_model_config
from study.run_audit import AuditError, RunAudit, read_json, write_json
from study.smoke import create_run, execute, decide, report, notebook, entry_id, verify_frozen


def local_fixture(audit):
    replies = read_json(ROOT / 'evaluation/study_smoke_local_responses.json')['responses']

    def reply(payload):
        context = json.loads(payload['messages'][1]['content'][0]['text'])
        matches = [r for r in audit.rows.values()
                   if r['context']['confirmed_question'] == context['confirmed_question']
                   and r['context']['student_work'] == context['student_work']]
        if len(matches) != 1:
            raise AuditError('没有匹配的手写响应，不能伪造成功。')
        return {'object': 'chat.completion', 'model': 'local-handwritten-fixture',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': json.dumps(replies[matches[0]['row_id']], ensure_ascii=False),
                'reasoning_content': 'DO_NOT_RECORD_INTERNAL_REASONING'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}
    return reply


def run_local(output, config=None):
    from http_test_support import LocalModelServer
    audit = create_run(output, mode='local_http_test', config=config)
    with LocalModelServer() as server:
        server.body = local_fixture(audit)
        first = execute(audit, server=server)
        if first.get('execution_error') or first['counts']['failed'] or first['counts']['running']:
            return first
        untouched = not notebook(audit).directory.exists()
        for row_id in ('b01', 'b02', 'b03', 'b04'):
            decide(audit, row_id, 'accept', actor='local_fixture')
        decide(audit, 'b05', 'reject', actor='local_fixture')
        second = execute(audit, server=server)
        if second.get('execution_error') or second['counts']['failed'] or second['counts']['running']:
            return second
        rejected_path = notebook(audit).path(entry_id(audit, 'b04'))
        before = rejected_path.read_bytes()
        decide(audit, 'b07', 'reject', actor='local_fixture')
        rejected_unchanged = before == rejected_path.read_bytes()
        decide(audit, 'b06', 'accept', actor='local_fixture')
        accepted_path = notebook(audit).path(entry_id(audit, 'b02'))
        once = accepted_path.read_bytes()
        decide(RunAudit(output), 'b06', 'accept', actor='local_fixture')
        checks = {'notebook_absent_before_confirmation': untouched,
            'rejection_kept_original_bytes': rejected_unchanged,
            'duplicate_confirmation_kept_bytes': once == accepted_path.read_bytes(),
            'reopen_preserved_correction': len(notebook(RunAudit(output)).get(entry_id(audit, 'b02'))['corrections']) == 1,
            'local_http_requests': len(server.requests), 'real_api_requests': 0,
            'decisions_are_simulated': True, 'quality_evaluated': False}
        write_json(audit.directory / 'local_checks.json', checks)
    return report(audit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='preview', choices=('preview', 'local', 'status', 'decide', 'run'))
    parser.add_argument('--output', type=Path, help='预览/本机演练的新报告目录，不能已存在')
    parser.add_argument('--directory', type=Path, help='继续读取的已有运行目录')
    parser.add_argument('--config', type=Path, help='预览或本机演练时冻结的无密钥配置')
    parser.add_argument('--rows', nargs='+', help='仅 preview 可用：选定请求编号，例如 b01 b02 b03；默认完整 7 次')
    parser.add_argument('--row', help='例如 b02')
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--accept', action='store_true')
    choice.add_argument('--reject', action='store_true')
    parser.add_argument('--max-requests', type=int, help='整份计划累计请求名额上限；不是本次进程新增数量')
    parser.add_argument('--confirm-plan', help='完整的预览计划 SHA-256')
    parser.add_argument('--budget-note', help='另行确认后的费用预算说明；程序限制请求数，不保证供应商金额封顶')
    args = parser.parse_args(argv)
    try:
        if args.rows is not None and args.action != 'preview':
            raise AuditError('请求范围只能在 preview 时选择，运行时不能修改。')
        if args.action in ('preview', 'local'):
            if args.output is None or args.directory is not None:
                raise AuditError('请提供新的 --output 目录。')
            config = load_model_config(args.config) if args.config else None
            result = (report(create_run(args.output, config=config, row_ids=args.rows)) if args.action == 'preview'
                      else run_local(args.output, config))
        else:
            if args.directory is None or args.output is not None or args.config is not None:
                raise AuditError('请提供已有 --directory；配置已冻结，不能在续跑时替换。')
            audit = RunAudit(args.directory)
            if args.action == 'status':
                # 状态查看只读，不新建账本或重发请求。
                result = audit.status()
            elif args.action == 'decide':
                if not args.row or args.accept == args.reject:
                    raise AuditError('必须指定 --row 和 --accept 或 --reject。')
                result = decide(audit, args.row, 'accept' if args.accept else 'reject')
            else:
                if audit.manifest['execution_mode'] != 'real_api':
                    raise AuditError('不能把本机模拟报告升级成真实报告。')
                if not args.confirm_plan or args.max_requests is None or not args.budget_note:
                    raise AuditError('先确认计划、累计请求上限和预算说明；没有发送请求。')
                verify_frozen(audit, live=True)
                if args.confirm_plan != audit.manifest['plan_id'] or not 1 <= args.max_requests <= len(audit.rows):
                    raise AuditError('计划编号或累计上限不正确；未读取密钥，未发送请求。')
                state = audit.status()
                if state['counts']['failed'] or state['counts']['running']:
                    raise AuditError('已有失败或完成情况未知的请求；不续发，不要求重新提供密钥。')
                if state['reserved_attempts'] >= args.max_requests:
                    print(json.dumps(state, ensure_ascii=False))
                    return 0
                key = os.environ.get('DEEPSEEK_API_KEY')
                if not key:
                    if not sys.stdin.isatty():
                        raise AuditError('请在本机交互终端隐藏输入密钥，不从命令参数或聊天读取。')
                    key = getpass.getpass('DeepSeek API 密钥（仅本次进程）：')
                result = execute(audit, key=key, max_requests=args.max_requests,
                                 confirm_plan=args.confirm_plan, budget_note=args.budget_note)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('execution_error') or result.get('counts', {}).get('failed') or result.get('counts', {}).get('running') else 0
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        # 不回显未知异常，防止路径/输入/服务错误正文夹带凭证。
        print('未执行或已停止：请检查计划、运行状态和参数；没有自动重试。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
