"""无密钥的 mock 演示入口；--check 只读，不启动服务或创建存档。"""
import argparse
import errno
import importlib
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def preflight(root):
    if sys.version_info < (3, 10):
        raise ValueError('需要 Python 3.10 或更新版本；本机已验证 3.12.5。')
    for name in ('app.py', 'model_config.example.json', 'requirements-lock.txt'):
        if not (root / name).is_file():
            raise ValueError(f'项目文件缺失：{name}。请保留完整项目目录。')
    # 显式使用示例配置，忽略本机 API 配置及继承的 MATH_MODEL_CONFIG。
    from legacy.model_api import load_model_config
    config = load_model_config(root / 'model_config.example.json')
    if config.mode != 'mock':
        raise ValueError('模拟入口只接受 mock 配置；请恢复 model_config.example.json 的 mode。')
    try:
        streamlit = importlib.import_module('streamlit')
    except ImportError:
        raise ValueError('Streamlit 或依赖不可用；请按 DEMO_QUICKSTART.md 安装锁定依赖。') from None
    expected = next(line.split('==', 1)[1] for line in
                    (root / 'requirements-lock.txt').read_text().splitlines()
                    if line.startswith('streamlit=='))
    if streamlit.__version__ != expected:
        raise ValueError(f'Streamlit 版本不符，需要 {expected}；请按 DEMO_QUICKSTART.md 安装锁定依赖。')
    state_dir = root / 'data' / 'demo'
    for directory in (root / 'data', state_dir):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError('data 或 data/demo 不是普通目录；未启动，未修改已有文件。')
    return streamlit.__version__, state_dir


def ensure_port_available(port):
    # 只绑定回环地址探测，不连接占用者、不停止其他进程；正式启动仍由 Streamlit 再次绑定。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f'端口 {port} 已被占用，未启动新服务。若是已有演示可继续使用；'
                                 '否则用 --port 指定其他端口。不会停止占用者。') from None
            raise


def launch_environment(root, state_dir):
    environment = os.environ.copy()
    for name in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY'):
        environment.pop(name, None)
    environment['MATH_MODEL_CONFIG'] = str(root / 'model_config.example.json')
    environment['MATH_ASSISTANT_DATA_DIR'] = str(state_dir)
    environment['MATH_ASSISTANT_START_VIEW'] = '四题练习'
    environment['MATH_PHOTO_OFFLINE'] = '1'
    environment['MATH_STUDY_DEMO'] = '0'
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只读环境检查；不联网、不探测端口、不写存档')
    parser.add_argument('--port', type=int, default=8514, help='本机端口，默认 8514')
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开系统浏览器')
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.port <= 65535:
            raise ValueError('端口须在 1 至 65535 之间。')
        version, state_dir = preflight(ROOT)
        print(f'环境检查通过：Python {sys.version.split()[0]} / Streamlit {version}', flush=True)
        print('模拟模式：固定规则回复，无需密钥，不调用真实模型。', flush=True)
        print(f'演示存档：{state_dir}（已有进度继续保留）', flush=True)
        if args.check:
            print(f'只读检查结束；未启动服务、未写存档。端口 {args.port} 的占用情况在启动时检查。')
            return 0
        ensure_port_available(args.port)
        print(f'页面：http://127.0.0.1:{args.port}/', flush=True)
        print('停止服务：在本终端按 Ctrl+C；下次双击可恢复已确认进度。', flush=True)
        command = [sys.executable, '-B', '-m', 'streamlit', 'run', str(ROOT / 'legacy/app.py'),
                   '--server.address', '127.0.0.1', '--server.port', str(args.port),
                   '--server.headless', str(args.no_browser).lower(),
                   '--browser.gatherUsageStats', 'false']
        # 进程替换使 Ctrl+C 直接交给服务；没有重试或第二个后台启动器。
        os.chdir(ROOT)
        os.execve(sys.executable, command, launch_environment(ROOT, state_dir))
    except (OSError, ValueError, StopIteration) as exc:
        print(f'未启动：{exc or "依赖锁文件缺少 Streamlit 版本。"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
