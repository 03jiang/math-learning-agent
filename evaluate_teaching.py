"""成对提示词评估：默认仅预览；本机 HTTP 演练与真实调用必须显式选择。"""
import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from core import LearningSettings, ValidationError
from curriculum import initial_snapshot
from evaluation_cases import load_cases
from http_test_support import LocalModelServer, chat_envelope
from model_api import ApiTutor, HttpTransport, api_key_variable, load_model_config, request_payload
from model_boundary import build_model_request
from retrieval import search_notes
from workflow import Workspace

ROOT = Path(__file__).resolve().parent


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path, value):
    """先写临时文件再替换；调用前的 running 标记不会被半份结果覆盖。"""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.eval-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def frozen_sources():
    paths = sorted([*ROOT.glob('*.py'), *ROOT.glob('study/*.py'), *ROOT.glob('study/*.swift'),
                    *ROOT.glob('prompts/*.txt'), *ROOT.glob('notes/*.json'),
                    ROOT / 'evaluation/teaching_cases.jsonl', ROOT / 'evaluation/RUBRIC.md',
                    ROOT / 'evaluation/PLAN.md', ROOT / 'requirements-lock.txt'])
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def make_plan(suite, repeats, config):
    if type(repeats) is not int or not 1 <= repeats <= 3:
        raise ValidationError('重复次数只能为 1 至 3')
    plan = []
    for case in load_cases(suite):
        snapshot = initial_snapshot(case['task_id'])
        settings = LearningSettings(**case['settings'])
        sources = search_notes(case['search_query'])
        for repeat in range(1, repeats + 1):
            variants = ['protocol'] if suite in ('smoke', 'revision') else ['baseline', 'protocol']
            # 相邻成对执行，交替先后顺序，减少固定调用次序的影响。
            if (len(plan) // 2 + repeat) % 2 == 0:
                variants.reverse()
            for variant in variants:
                request = build_model_request(case['message'], snapshot, settings, sources,
                    answer_submission=case['answer_submission'], solution_steps=case['solution_steps'],
                    reading_selections=case['reading_selections'], prompt_variant=variant,
                    help_action=case.get('help_action'))
                plan.append({'row_id': f'r{len(plan) + 1:03d}', 'case': case, 'variant': variant,
                             'repeat': repeat, 'request': request, 'payload': request_payload(config, request)})
    return plan


def run_batch(output, *, mode='preview', suite='all', repeats=1, config=None, api_key='',
              max_requests=None, resume=False, fixture=None):
    if mode not in ('preview', 'local_http_test', 'real_api'):
        raise ValidationError('未知评估模式')
    if fixture is not None and mode != 'local_http_test':
        raise ValidationError('手写响应只能用于本机演练')
    config = config or load_model_config(ROOT / 'model_config.deepseek.example.json')
    if config.mode != 'api' or config.provider != 'deepseek' or config.api_format != 'chat_completions':
        raise ValidationError('本评估入口需要 DeepSeek chat_completions API 配置')
    plan = make_plan(suite, repeats, config)
    if mode == 'real_api':
        if type(max_requests) is not int or not 1 <= max_requests <= len(plan):
            raise ValidationError('真实评估须明确 --max-requests，本次尝试数不能超过用例计划')
        ApiTutor(config, api_key)  # 密钥格式预检查，不发请求，不创建报告
    elif max_requests is not None and (type(max_requests) is not int or not 1 <= max_requests <= len(plan)):
        raise ValidationError('本次请求上限不合法')
    if suite in ('smoke', 'revision') and repeats != 1:
        raise ValidationError('接口验证与修订回归固定每例一次')
    expected = {'schema_version': 1, 'mode': mode, 'suite': suite, 'repeats': repeats,
                'config': asdict(config), 'source_hashes': frozen_sources(), 'plan': plan,
                'planned_requests': 0 if mode == 'preview' else len(plan)}
    output = Path(output)
    if output.is_symlink():
        raise ValidationError('报告目录不能是符号链接')
    if resume:
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        if {k: v for k, v in manifest.items() if k != 'created_at'} != expected:
            raise ValidationError('配置、用例、提示词或代码已改变，不能续跑这份冻结报告')
        for row in plan:
            saved = json.loads((output / 'requests' / f"{row['row_id']}.json").read_text(encoding='utf-8'))
            if saved != {'request': row['request'], 'payload': row['payload']}:
                raise ValidationError('已冻结的请求文件发生变化')
    else:
        output.mkdir(parents=True, exist_ok=False)
        (output / 'requests').mkdir()
        (output / 'responses').mkdir()
        write_json(output / 'manifest.json', {**expected, 'created_at': datetime.now(timezone.utc).isoformat()})
        for row in plan:
            write_json(output / 'requests' / f"{row['row_id']}.json", {'request': row['request'], 'payload': row['payload']})
        from evaluation_scoring import create_scorecard
        create_scorecard(output, expected)
    attempted_this_run = 0
    cap = max_requests or len(plan)
    with ExitStack() as stack:
        lock = stack.enter_context((output / '.run.lock').open('a'))
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValidationError('同一报告目录已有评估进程在运行') from None
        server = None
        if mode == 'local_http_test':
            server = stack.enter_context(LocalModelServer())
            server.body = fixture or chat_envelope
        for row in plan:
            path = output / 'responses' / f"{row['row_id']}.json"
            if path.exists():
                continue  # 包含失败及 running；完成情况未知时绝不自动重复调用
            if attempted_this_run >= cap:
                break
            report = {'row_id': row['row_id'], 'mode': mode, 'status': 'preview' if mode == 'preview' else 'running',
                      'request_hash': digest(row['request']), 'round': None, 'model_calls': [],
                      'real_api_calls': 0 if mode != 'real_api' else None,
                      'state_unchanged': None, 'error_code': None}
            write_json(path, report)
            if mode == 'preview':
                continue
            attempted_this_run += 1
            tutor = ApiTutor(config, 'local-test-key' if server else api_key,
                             transport=HttpTransport(server.chat_url) if server else None,
                             prompt_variant=row['variant'])
            with tempfile.TemporaryDirectory(prefix='math-evaluation-') as directory:
                workspace = Workspace(directory, tutor=tutor)
                before = {key: assistant.snapshot() for key, assistant in workspace.assistants.items()}
                case = row['case']
                result = workspace.run(row['row_id'], case['task_id'], case['message'], case['settings'],
                                       answer_submission=case['answer_submission'], solution_steps=case['solution_steps'],
                                       reading_selections=case['reading_selections'], search_query=case['search_query'],
                                       help_action=case.get('help_action'))
                unchanged = (all(assistant.snapshot() == before[key] for key, assistant in workspace.assistants.items())
                             and not list(Path(directory).glob('*.json')))
                report.update(status='reply_valid' if result.reply and not result.error and unchanged else 'failed',
                              round=asdict(result), model_calls=result.model_calls,
                              real_api_calls=result.real_api_calls, state_unchanged=unchanged,
                              error_code=result.model_calls[-1]['error_code'] if result.model_calls else None)
                if result.model_calls and result.model_calls[-1].get('request_hash') != report['request_hash']:
                    report.update(status='failed', error_code='request_changed')
                write_json(path, report)
            if report['status'] == 'failed':
                break  # 先处理失败；余下行可显式续跑，已尝试行一律不重发
    summary = batch_status(output)
    write_json(output / 'run_status.json', summary)
    return summary


def batch_status(output):
    manifest = json.loads((Path(output) / 'manifest.json').read_text(encoding='utf-8'))
    counts = {key: 0 for key in ('preview', 'reply_valid', 'failed', 'running', 'pending')}
    known_real = 0
    for row in manifest['plan']:
        path = Path(output) / 'responses' / f"{row['row_id']}.json"
        if not path.exists():
            counts['pending'] += 1
            continue
        report = json.loads(path.read_text(encoding='utf-8'))
        counts[report['status']] += 1
        known_real += report['real_api_calls'] or 0
    return {'mode': manifest['mode'], 'suite': manifest['suite'], 'planned_rows': len(manifest['plan']),
            'counts': counts, 'known_real_api_attempts': known_real,
            'real_api_calls_unknown': manifest['mode'] == 'real_api' and counts['running'] > 0,
            'quality_scores': '尚需人工评分；接口校验通过不代表教学正确'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--local-http', action='store_true')
    modes.add_argument('--live', action='store_true')
    parser.add_argument('--suite', choices=['all', 'development', 'reserved', 'smoke', 'revision'], default='all')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--max-requests', type=int)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.config and not args.live:
            raise ValidationError('--config 仅用于显式 --live；预览和本机演练不读取真实配置')
        if args.live and args.max_requests is None:
            raise ValidationError('真实调用必须显式指定 --max-requests')
        config, key = None, ''
        if args.live:
            config = load_model_config(args.config or ROOT / 'model_config.deepseek.example.json')
            key = os.environ.get(api_key_variable(config), '')
            if not key and sys.stdin.isatty():
                key = getpass.getpass('DeepSeek API 密钥（隐藏输入，仅本次进程使用）：')
        summary = run_batch(args.output, mode='real_api' if args.live else 'local_http_test' if args.local_http else 'preview',
                            suite=args.suite, repeats=args.repeats, config=config, api_key=key,
                            max_requests=args.max_requests, resume=args.resume)
        print(json.dumps(summary, ensure_ascii=False))
        return 1 if any(summary['counts'][name] for name in ('failed', 'running', 'pending')) else 0
    except (OSError, ValueError) as exc:
        # 错误详情可能来自外部输入；只展示本地验证错误的固定文案。
        print(str(exc) if isinstance(exc, ValidationError) else '评估未完成：检查配置、密钥和报告目录；不自动重试。', file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('已中止。保留 running 的行完成情况未知；续跑会跳过，绝不自动重发。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
