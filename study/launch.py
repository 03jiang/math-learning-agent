"""启动拍照错题本；真实请求只能由页面按钮触发。"""
import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.launch_demo import preflight, ensure_port_available


def launch_environment(root, *, demo=False):
    env=os.environ.copy()
    env['MATH_MODEL_CONFIG']=str(root/'model_config.example.json')
    env['MATH_ASSISTANT_DATA_DIR']=str(root/'data'/('demo-study' if demo else 'study'))
    env['MATH_ASSISTANT_START_VIEW']='拍照解题'
    env['MATH_PHOTO_OFFLINE']='1' if demo else '0'
    env['MATH_STUDY_DEMO']='1' if demo else '0'
    if demo:
        for name in ('DEEPSEEK_API_KEY','OPENAI_API_KEY'): env.pop(name,None)
    return env


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8518)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--demo',action='store_true',help='固定方程题的完整离线回放，使用独立存档，不调用模型')
    args=parser.parse_args(argv)
    try:
        preflight(ROOT)
        if not 1<=args.port<=65535: raise ValueError('端口不合法。')
        if args.check:
            print('环境可启动；'+('完整离线演示，不需要密钥。' if args.demo else 'DeepSeek 识图与分析需要密钥。')+'本次零请求。')
            return 0
        ensure_port_available(args.port)
        env=launch_environment(ROOT,demo=args.demo)
        print(f'拍照错题本：http://127.0.0.1:{args.port}/\n存档：{env["MATH_ASSISTANT_DATA_DIR"]}/notebook\n'
              +('离线演示仅回放自带方程题，不调用模型。Ctrl+C 停止。' if args.demo else
                '在侧栏输入 DeepSeek 密钥；点击识图或分析才发送，可能计费。Ctrl+C 停止。'),flush=True)
        os.chdir(ROOT)
        os.execve(sys.executable,[sys.executable,'-B','-m','streamlit','run',str(ROOT/'app.py'),
            '--server.address','127.0.0.1','--server.port',str(args.port),'--server.headless',str(args.no_browser).lower(),
            '--browser.gatherUsageStats','false'],env)
    except (OSError,ValueError) as exc:
        print(f'未启动：{exc}',file=sys.stderr)
        return 2


if __name__=='__main__': raise SystemExit(main())
