"""启动数学助手。API 模式缺少环境密钥时，只在交互终端隐藏输入。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

from legacy.model_api import ROOT, ApiTutor, ModelAPIError, api_key_variable, load_model_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, help='无密钥的模型配置 JSON')
    parser.add_argument('--port', type=int, default=8504)
    parser.add_argument('--check', action='store_true', help='仅检查配置，不调用 API、不启动服务')
    args = parser.parse_args()
    try:
        config = load_model_config(args.config)
        if args.check:
            if config.mode == 'api' and os.environ.get(api_key_variable(config)):
                ApiTutor(config, os.environ[api_key_variable(config)])
            print(json.dumps({**asdict(config), 'api_key_present': bool(os.environ.get(api_key_variable(config))),
                              'ready': config.mode == 'mock' or bool(os.environ.get(api_key_variable(config)))}, ensure_ascii=False, indent=2))
            return 0 if config.mode == 'mock' or os.environ.get(api_key_variable(config)) else 2
        if not 1 <= args.port <= 65535:
            raise ValueError('端口须在 1 至 65535 之间')
        environment = os.environ.copy()
        if args.config:
            environment['MATH_MODEL_CONFIG'] = str(args.config.resolve())
        if config.mode == 'api':
            if not environment.get(api_key_variable(config)):
                if not sys.stdin.isatty():
                    raise ModelAPIError('missing_key')
                environment[api_key_variable(config)] = getpass.getpass('本次启动的 模型 API 密钥（隐藏输入，不保存文件）：')
            ApiTutor(config, environment[api_key_variable(config)])  # 仅校验，不发送请求
            print(f'API 模式：{config.provider}/{config.model}；提交求助会发送当前问题与任务信息，并可能产生费用。')
        else:
            print('模拟模式：不会调用真实 API。')
        return subprocess.run([sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'legacy/app.py'),
                               '--server.address', '127.0.0.1', '--server.port', str(args.port)],
                              cwd=ROOT, env=environment).returncode
    except (ValueError, OSError) as exc:
        print(f'未启动：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
