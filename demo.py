"""小学分数与应用题的完整终端演示；只使用临时目录。"""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from workflow import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix='primary-math-demo-') as folder:
        workspace = Workspace(folder)
        examples = [
            ('fraction-add', '我不明白为什么要通分，请给小提示'),
            ('fraction-add', '答案：2/6'),
            ('fraction-add', '答案：6/8'),
            ('fraction-word', '答案：3/4'),
            ('fraction-word', '请直接讲解'),
            ('fraction-word', '查笔记：所求量'),
            ('fraction-word', '查笔记：行星轨道'),
        ]
        results = []
        for number, (task_id, message) in enumerate(examples):
            result = workspace.run(f'demo-{number}', task_id, message)
            results.append(result)
            print(json.dumps({'task_id': task_id, 'input': message, 'result': asdict(result)}, ensure_ascii=False, indent=2))
        correct = results[2]
        workspace.decide('fraction-add', correct.proposal, 'accept')
        workspace.decide('fraction-word', results[3].proposal, 'reject')
        edited = workspace.run('edit', 'fraction-word', '先帮我理解题意')
        workspace.decide('fraction-word', edited.proposal, 'edit', edited_state={'current_step': '先圈出题目问的剩余部分'})
        restored = Workspace(folder)
        for task_id in restored.assistants:
            print('恢复状态：', json.dumps(asdict(restored.assistant(task_id).snapshot()), ensure_ascii=False))
        print('正确答案接受后，已完成列表：', restored.assistant('fraction-add').snapshot().task.completed_steps)
        print('日志行数：', len((Path(folder) / 'runs.jsonl').read_text().splitlines()))
        print('真实模型调用数：0；以上均为数学规则模拟。')


if __name__ == '__main__':
    main()
