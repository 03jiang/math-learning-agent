"""以手写 JSON 回放模型接口；临时存档，可导出本次合成示例的请求。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import argparse
import json
from pathlib import Path
import tempfile

from legacy.model_boundary import ReplayTutor
from legacy.workflow import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--export-request', type=Path, help='只新建请求预览文件，不覆盖已有文件')
    args = parser.parse_args()
    raw = (Path(__file__).resolve().parents[1] / 'examples' / 'model_reply.json').read_text(encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='math-model-boundary-') as directory:
        root = Path(directory)
        print('模型接入边界演示：手写 JSON 回放，无真实模型、无网络 API、无密钥。')
        invalid = json.loads(raw)
        invalid['completed_steps'] = ['已经掌握']
        failed = Workspace(root / 'bad', tutor=ReplayTutor(json.dumps(invalid, ensure_ascii=False)))
        result = failed.run('invalid', 'fraction-add', '', answer_submission='3/4', solution_steps='1/2=2/6')
        assert result.error and result.reply is None and result.proposal is None
        assert result.local_checks['answer']['status'] == 'correct'
        assert result.local_checks['steps']['status'] == 'incorrect'
        assert not (root / 'bad' / 'fraction-add.json').exists()
        print('\n【拒绝越权回复】', result.error)
        print('本地结果仍在：最终答案数值相符，第一行等式有误；没有进度建议或状态文件。')

        tutor = ReplayTutor(raw)
        workspace = Workspace(root / 'valid', tutor=tutor)
        first = workspace.run('first', 'fraction-add', '查笔记：通分')
        assert first.error is None and first.proposal is not None
        path = root / 'valid' / 'fraction-add.json'
        print('\n【合法回复】', first.reply.source)
        print(first.reply.explanation)
        print('已核对来源 ID：', ', '.join(first.reply.cited_source_ids))
        assert not path.exists()
        workspace.decide('fraction-add', first.proposal, 'reject')
        assert not path.exists()
        print('用户拒绝更新后，仍无状态文件。')

        second = workspace.run('second', 'fraction-add', '查笔记：通分')
        workspace.decide('fraction-add', second.proposal, 'accept')
        before = path.read_bytes()
        assert workspace.decide('fraction-add', second.proposal, 'accept')[0] == 'already_applied'
        assert before == path.read_bytes()
        print('\n【接受更新】只保存当前步骤，版本为 1；重复确认没有重写文件。')

        editable = json.loads(raw)
        editable['proposed_configuration_update'] = {'pattern_guidance': 'on'}
        tutor.response_json = json.dumps(editable, ensure_ascii=False)
        third = workspace.run('third', 'fraction-add', '查笔记：通分')
        assert third.proposal.proposed_state_update is None
        workspace.decide('fraction-add', third.proposal, 'edit', edited_configuration={'scope_support': 'connected'})
        saved = workspace.assistant('fraction-add').snapshot()
        assert saved.settings.scope_support == 'connected'
        assert saved.settings.pattern_guidance == 'off'
        assert saved.task.completed_steps == []
        assert saved.metadata.version == 2
        assert Workspace(root / 'valid').assistant('fraction-add').snapshot() == saved
        print('\n【编辑后确认】改为开启知识联系，其他设置不变；版本为 2，已完成列表仍为空。')
        print('重新加载恢复成功。')

        if args.export_request:
            args.export_request.parent.mkdir(parents=True, exist_ok=True)
            with args.export_request.open('x', encoding='utf-8') as stream:
                json.dump(tutor.requests[0], stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            print('\n合成示例请求已导出：', args.export_request)
        print('\n所有演示断言通过；临时目录自动清理，真实 API 调用数为 0。')


if __name__ == '__main__':
    main()
