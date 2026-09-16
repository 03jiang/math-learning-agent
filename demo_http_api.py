"""使用本机手写响应演示 HTTP 接入；不需要密钥，不连接 OpenAI。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from http_test_support import LocalModelServer
from model_api import ApiTutor, HttpTransport, ModelConfig
from workflow import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix='math-http-demo-') as directory, LocalModelServer() as server:
        root = Path(directory)
        config = ModelConfig(mode='api', model='fixture-model', timeout_seconds=2)
        tutor = ApiTutor(config, 'local-test-key', transport=HttpTransport(server.url))
        workspace = Workspace(root, tutor=tutor)
        first = workspace.run('success', 'fraction-add', '请帮助我理解通分')
        assert first.error is None and first.real_api_calls == 0
        print('本机 HTTP 接入演示：手写响应，无真实模型调用。')
        print('\n【成功】', first.reply.source)
        print(first.reply.explanation)
        print('返回的测试用量：', json.dumps(first.model_calls[0]['usage']))
        print('这组 token 数为手写测试数据，费用未知。')
        assert not (root / 'fraction-add.json').exists()
        workspace.decide('fraction-add', first.proposal, 'accept')
        before = (root / 'fraction-add.json').read_bytes()
        assert workspace.decide('fraction-add', first.proposal, 'accept')[0] == 'already_applied'
        assert workspace.run('success', 'fraction-add', '请帮助我理解通分') is first
        assert len(server.requests) == 1
        assert before == (root / 'fraction-add.json').read_bytes()
        print('接受更新后版本为 1，已完成列表为空；重跑和重复确认都没有再次发请求。')

        server.status = 429
        second = workspace.run('limited', 'fraction-add', '', answer_submission='3/4')
        assert second.proposal is None and second.local_checks['answer']['status'] == 'correct'
        assert second.model_calls[0]['error_code'] == 'http_429'
        print('\n【限流】', second.error)
        print('本地数值检查仍保留，存档没有改变。')

        server.status = 200
        server.stall_body = True
        server.delay = 3
        tutor.config = replace(config, timeout_seconds=1)
        third = workspace.run('timeout', 'fraction-add', '帮助我')
        assert third.model_calls[0]['error_code'] == 'timeout'
        assert third.model_calls[0]['completion_unknown']
        assert third.proposal is None
        print('\n【超时】', third.error)
        print('耗时毫秒：', third.model_calls[0]['elapsed_ms'])
        print('请求已尝试，服务端是否完成无法确定；不自动重试。')
        assert before == (root / 'fraction-add.json').read_bytes()
        assert workspace.assistant('fraction-add').snapshot().task.completed_steps == []
        assert Workspace(root).assistant('fraction-add').snapshot() == workspace.assistant('fraction-add').snapshot()
        print('\n本机 HTTP 请求共 3 次，真实 API 请求 0 次。全部演示断言通过，临时存档自动清理。')


if __name__ == '__main__':
    main()
