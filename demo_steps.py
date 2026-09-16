"""解题步骤与变式题终端验收；临时数据不写入个人存档。"""
from dataclasses import asdict
import json
import tempfile
from workflow import Workspace


def main():
    examples = [
        ('fraction-add', '1/2=2/4\n2/4+1/4=2/6', '3/4'),
        ('fraction-add', '1/2+1/4=3/4', None),
        ('fraction-word', '剩余：1/2+1/4=3/4', None),
        ('fraction-word', '1-1/2-1/4=1/4', None),
        ('fraction-add-thirds', '1/3=2/6\n2/6+1/6=3/6=1/2', None),
        ('fraction-word-eighths', '1-3/8-1/4=3/8', None),
        ('fraction-add', '我已经全都理解了', None),
    ]
    with tempfile.TemporaryDirectory(prefix='math-steps-demo-') as directory:
        workspace=Workspace(directory)
        for index,(task,steps,answer) in enumerate(examples):
            result=workspace.run(str(index),task,'',solution_steps=steps,answer_submission=answer)
            print(json.dumps(asdict(result),ensure_ascii=False,indent=2))
            if index==1:
                workspace.decide(task,result.proposal,'accept')
                print('接受后已完成步骤：',workspace.assistant(task).snapshot().task.completed_steps)
        print('真实模型调用数：0；步骤核对不自动标记掌握。')


if __name__=='__main__':
    main()
