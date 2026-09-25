"""零密钥、三次本机手写 HTTP 回复；演示设置确认、同题追问和换题隔离。"""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from legacy.http_test_support import LocalModelServer
from study.agent import AgentStudyService
from study.agent_tools import ToolScope
from study.context import task_for,learning,remember,select_next
from study.example import QUESTION,STUDENT_WORK,ANALYSIS
from study.preferences import LocalPreferences,DEFAULTS
from study.service import PhotoTransport
from study.smoke import default_config
from tools.demo_study_agent import envelope

REPLY={'schema_version':1,'status':'explained','reply':'等式两边同时减去 3，仍然相等。',
       'next_step':'自己写出两边同时减去 3 后的等式。','clarification':''}


def run(directory):
    directory=Path(directory)
    if directory.exists() or directory.is_symlink():raise ValueError('演练目录必须不存在；不会覆盖。')
    directory.mkdir(parents=True,mode=0o700)
    store=LocalPreferences(directory/'local-state');before=store.load()
    preview=learning(task_for(None,'self-written-equation','original','history-off'),before['settings'],
                     request='这次详细一点',overrides={'presentation_density':'detailed'})
    unconfirmed_did_not_write=not store.path.exists()
    confirmed=store.save(0,{**DEFAULTS,'explanation_mode':'example'})
    saved_bytes=store.path.read_bytes()
    task=task_for(None,'self-written-equation','original','history-off')
    with LocalModelServer() as server:
        def tutor():return AgentStudyService(default_config(),'local-test-key',transport=PhotoTransport(server.chat_url),
            scope=ToolScope(directory/'notebook'),audit_dir=directory/'agent-runs')
        server.body=envelope(result=ANALYSIS,citations=[])
        result=tutor().analyze(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=learning(task,confirmed['settings'],
                              request=preview['turn_request']['text'],overrides=preview['temporary_overrides']))
        remember(task,'analysis','分析原作答',result['summary'],'人工 HTTP 回放，非模型实测')
        server.body=envelope(result=REPLY,citations=[])
        coach=tutor().coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=learning(task,confirmed['settings'],intent='hint'))
        remember(task,'hint','只给一个提示',coach['reply'],'人工 HTTP 回放，非模型实测')
        select_next(task,coach);selected=deepcopy(task)
        tutor().coach(QUESTION,'初中',STUDENT_WORK,work_kind='steps',learning=learning(task,confirmed['settings'],intent='explain',request='请解释待做任务'))
        payloads=[r['payload'] for r in server.requests]
    other=task_for(task,'new-question','new-work','history-off')
    contexts=[json.loads(p['messages'][1]['content'][0]['text']) for p in payloads]
    summary={'mode':'local_http_test','real_api_calls':0,'http_requests':len(payloads),
        'unconfirmed_did_not_write':unconfirmed_did_not_write,
        'temporary_density':contexts[0]['learning']['effective_settings']['presentation_density'],
        'next_round_density':contexts[1]['learning']['effective_settings']['presentation_density'],
        'long_term_unchanged_by_turns':store.path.read_bytes()==saved_bytes,
        'settings_restored':LocalPreferences(store.directory).load()==confirmed,
        'followup_included_turns':len(contexts[2]['learning']['recent_dialogue']),
        'selected_status':selected['status'],'completed_steps':selected['completed_steps'],
        'new_question_dialogue':other['turns'],'new_question_next_step':other['selected_next_step'],
        'notebook_created':(directory/'notebook').exists(),'teaching_quality':'not_evaluated'}
    (directory/'requests.json').write_text(json.dumps(payloads,ensure_ascii=False,indent=2)+'\n')
    (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:print(json.dumps(run(args.output),ensure_ascii=False))
    except (ValueError,OSError) as exc:print(str(exc));return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
