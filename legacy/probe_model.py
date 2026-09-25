"""一道合成例题的接口验证。默认只预览；真实调用必须显式指定 --live。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from legacy.core import ValidationError
from legacy.curriculum import initial_snapshot
from legacy.http_test_support import LocalModelServer
from legacy.model_api import ApiTutor, HttpTransport, ModelAPIError, ModelConfig, api_key_variable, load_model_config
from legacy.model_boundary import build_model_request
from legacy.workflow import Workspace

ROOT = Path(__file__).resolve().parents[1]
CASE = {'case_id': 'single-fraction-hint-v1', 'synthetic': True, 'task_id': 'fraction-add',
        'message': '我把分子和分母分别相加了，但不理解为什么不对，请给一个小提示。',
        'answer_submission': '2/6', 'solution_steps': ''}
SOURCES = ('prompts/protocol.txt', 'prompts/baseline.txt', 'prompts/output_contract.txt', 'legacy/reading_check.py', 'legacy/probe_model.py', 'legacy/core.py', 'legacy/curriculum.py', 'legacy/step_check.py', 'legacy/workflow.py',
            'legacy/retrieval.py', 'legacy/model_boundary.py', 'legacy/teaching_guard.py', 'legacy/model_api.py', 'legacy/http_worker.py',
           'legacy/http_test_support.py', 'examples/model_reply.json')
REVIEW_ITEMS = {
    'math_correctness': '分数单位、同一整体和通分解释是否正确？有无错误算式？',
    'difficulty_match': '是否回应了把分子、分母分别相加的困难？有无误读学生答案？',
    'hint_size': '是否先给一个可操作的小提示，并避免直接公布最终答案？',
    'settings_match': '帮助是否符合请求中的六项有效设置？',
    'claims_and_sources': '是否冒称学生已掌握、已经完成，或编造来源？',
}


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def review_text(report):
    lines = ['# 单轮教学回复人工检查', '', f"运行类型：{report['mode']}；结果：{report['status']}。", '',
             '本表尚未评分。程序检查通过不代表解释正确或教学有效。',
             '先阅读 request.json 和 result.json；每项填写“符合 / 不符合 / 无法判断”，并引用具体文字。',
             '本机 HTTP 的回复和 token 数均为手写测试数据，不能评价真实模型。', '',
             '评阅人：____　日期：____', '']
    for title in REVIEW_ITEMS.values():
        lines += [f'- {title}', '  判断：____　证据：____']
    lines += ['', '是否需要修改教学提示或内容：____',
              '改动依据与复测计划：____', '',
              '此例已用于调试，不是独立保留集，也没有普通教学提示词基线对照。',
              '不能用单轮结果证明学习效果或计算模型准确率。', '']
    return '\n'.join(lines)


def probe(output, *, mode='preview', config=None, api_key=''):
    """写入全新的报告目录；临时任务仅生成建议，从不调用 confirm/decide。"""
    if mode not in ('preview', 'local_http_test', 'real_api'):
        raise ValidationError('未知验证模式')
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValidationError('报告目录已存在，请使用新名称；不会覆盖或重新调用')
    tutor = None
    if mode == 'real_api':
        if config is None or config.mode != 'api':
            raise ValidationError('真实联调需要明确的 api 模式和模型配置')
        tutor = ApiTutor(config, api_key)  # 无密钥时在创建报告和请求之前失败
    elif mode == 'local_http_test':
        config = ModelConfig(mode='api', model='fixture-model', timeout_seconds=2)
    else:
        config = None  # 预览不读取配置、环境密钥或日常任务

    snapshot = initial_snapshot(CASE['task_id'])
    request = build_model_request(CASE['message'], snapshot, snapshot.settings, [],
                                  answer_submission=CASE['answer_submission'], solution_steps=CASE['solution_steps'])
    output.mkdir(parents=True, exist_ok=False)  # 抢先占用新目录；同名再次执行不能发请求
    manifest = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
                'mode': mode, 'case': CASE, 'model_config': asdict(config) if config else None,
                'code_hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES},
                'request_sha256': hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'request.json', request)
    report = {'schema_version': 1, 'mode': mode, 'status': 'preview' if mode == 'preview' else 'running',
              'real_api_calls': 0 if mode != 'real_api' else None, 'model_calls': [], 'round': None,
              'state_unchanged': None, 'cost': None, 'usage_is_synthetic': mode == 'local_http_test',
              'human_review': {name: None for name in REVIEW_ITEMS}, 'error_code': None}
    # 若进程异常终止，此文件保留 running，次数未知；不能把它算作成功或零次调用。
    write_json(output / 'result.json', report)
    if mode != 'preview':
        try:
            with ExitStack() as stack:
                if mode == 'local_http_test':
                    server = stack.enter_context(LocalModelServer())
                    tutor = ApiTutor(config, 'local-test-key', transport=HttpTransport(server.url))
                directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='math-single-probe-')))
                workspace = Workspace(directory, tutor=tutor)
                before = {key: item.snapshot() for key, item in workspace.assistants.items()}
                result = workspace.run(CASE['case_id'], CASE['task_id'], CASE['message'],
                                       answer_submission=CASE['answer_submission'], solution_steps=CASE['solution_steps'])
                unchanged = (all(item.snapshot() == before[key] for key, item in workspace.assistants.items())
                             and not list(directory.glob('*.json')))
                report.update(round=asdict(result), model_calls=result.model_calls,
                              real_api_calls=result.real_api_calls, state_unchanged=unchanged,
                              status='reply_valid' if result.reply and not result.error and unchanged else 'failed',
                              error_code=result.model_calls[-1]['error_code'] if result.model_calls else None)
        except (OSError, ValueError):
            # 不输出路径、异常正文或凭证；若调用已有记录则保留已知的实际计数。
            calls = tutor.call_records if tutor else []
            report.update(status='failed', error_code='probe_error', model_calls=calls,
                          real_api_calls=sum(row['attempted_requests'] for row in calls if row['kind'] == 'real_api'))
        write_json(output / 'result.pending.json', report)
        os.replace(output / 'result.pending.json', output / 'result.json')
    (output / 'review.md').write_text(review_text(report), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='全新的报告目录；已存在则拒绝运行')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--local-http', action='store_true', help='本机手写响应，不调用真实模型')
    mode.add_argument('--live', action='store_true', help='向已配置模型提交一次合成例题，可能产生费用')
    parser.add_argument('--config', type=Path, help='仅 --live 使用的无密钥模型配置')
    args = parser.parse_args(argv)
    try:
        if args.config and not args.live:
            raise ValidationError('--config 仅用于 --live；预览和本机测试不读取真实配置')
        if args.output.exists() or args.output.is_symlink():
            raise ValidationError('报告目录已存在，请使用新名称；不会覆盖或重新调用')
        config, key = None, ''
        if args.live:
            config = load_model_config(args.config)
            if config.mode != 'api':
                raise ValidationError('--live 需要 mode=api 并填写准确模型 ID')
            key = os.environ.get(api_key_variable(config), '')
            if not key:
                if not sys.stdin.isatty():
                    raise ModelAPIError('missing_key')
                key = getpass.getpass('本次单轮验证的 模型 API 密钥（隐藏输入，不保存）：')
            print('即将提交一次合成例题，可能产生费用；不自动重试，不确认学习进度。', flush=True)
        chosen = 'real_api' if args.live else 'local_http_test' if args.local_http else 'preview'
        report = probe(args.output, mode=chosen, config=config, api_key=key)
        print(json.dumps({key: report[key] for key in ('mode', 'status', 'real_api_calls', 'state_unchanged')}, ensure_ascii=False))
        return 0 if report['status'] in ('preview', 'reply_valid') else 1
    except (ValueError, OSError):
        print('未完成：请检查模式、无密钥配置、密钥是否在本机提供，以及报告目录是否全新且可写；不会自动重试。', file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('已中止；若 result.json 保留 running，调用完成情况未知，请勿把它视为成功。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
