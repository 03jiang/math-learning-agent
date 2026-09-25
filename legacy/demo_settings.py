"""六项设置终端演示；使用临时目录，不修改日常学习存档。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
from dataclasses import asdict
import json
from pathlib import Path
import tempfile

from legacy.workflow import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix='math-settings-demo-') as directory:
        root = Path(directory)
        workspace = Workspace(root)
        task_id = 'fraction-word'
        assistant = workspace.assistant(task_id)
        path = root / f'{task_id}.json'
        original = assistant.snapshot()
        print('小学数学：同一道彩带应用题，对比三项新设置（固定规则，真实模型调用 0 次）')
        for index, (label, overrides) in enumerate([
            ('默认帮助', {}),
            ('只改变组织方式', {'structure_level': 'guided'}),
            ('只开启方法提示', {'pattern_guidance': 'on'}),
            ('只开启知识联系', {'scope_support': 'connected'}),
        ]):
            result = workspace.run(str(index), task_id, '帮我读懂题意', overrides)
            print(f'\n【{label}】\n{result.reply.explanation}')
            assert not path.exists()
            if result.proposal:
                workspace.decide(task_id, result.proposal, 'reject')
        print('\n未确认的帮助和拒绝的步骤建议均未创建状态 JSON。')

        chosen = {'step_size': 'medium', 'explanation_mode': 'hint', 'presentation_density': 'detailed',
                  'structure_level': 'guided', 'pattern_guidance': 'on', 'scope_support': 'connected'}
        proposal = assistant.propose(configuration_patch=chosen)
        assert not path.exists()
        workspace.decide(task_id, proposal, 'accept')
        print('\n【接受长期设置】', json.dumps(asdict(assistant.snapshot().settings), ensure_ascii=False))
        assert assistant.snapshot().task == original.task
        saved = path.read_bytes()
        assert workspace.decide(task_id, proposal, 'accept')[0] == 'already_applied'
        assert saved == path.read_bytes()
        print('重复确认：文件不变，版本仍为 1；已完成步骤仍为空。')

        proposal = assistant.propose(configuration_patch={'scope_support': 'focused', 'pattern_guidance': 'off'})
        workspace.decide(task_id, proposal, 'edit', edited_configuration={'scope_support': 'focused'})
        assert assistant.snapshot().settings.pattern_guidance == 'on'
        print('\n【编辑后保存】只保存“聚焦本题”，保留已开启的方法提示。版本为 2。')

        saved = path.read_bytes()
        proposal = assistant.propose(configuration_patch={'structure_level': 'free'})
        workspace.decide(task_id, proposal, 'reject')
        assert saved == path.read_bytes()
        print('\n【拒绝长期设置】拒绝改成自然说明，文件逐字节不变。')
        restored = Workspace(root).assistant(task_id).snapshot()
        assert restored == assistant.snapshot()
        assert restored.task.completed_steps == []
        assert not (root / 'fraction-add.json').exists()
        print('\n【重新加载】六项设置与版本恢复成功，其他任务无状态文件。')
        print(json.dumps(asdict(restored), ensure_ascii=False, indent=2))
        print('\n演示断言全部通过。临时目录自动清理，日常学习存档未改动。')


if __name__ == '__main__':
    main()
