"""仅供自动测试和终端演示的本机 HTTP 服务；响应为手写样例。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


def successful_envelope():
    reply = json.loads((Path(__file__).parent / 'examples/model_reply.json').read_text())
    reply['cited_source_ids'] = []
    return {'id': 'resp_local_fixture', 'model': 'fixture-model-v1', 'status': 'completed',
            'output': [{'type': 'reasoning', 'summary': []},
                       {'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': json.dumps(reply, ensure_ascii=False)}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}}


class LocalModelServer:
    def __init__(self):
        self.body = successful_envelope()
        self.status = 200
        self.delay = 0
        self.stall_body = False
        self.location = None
        self.requests = []

    def __enter__(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'),
                                       'payload': json.loads(raw)})
                response = owner.body(json.loads(raw)) if callable(owner.body) else owner.body
                body = response if isinstance(response, bytes) else json.dumps(response, ensure_ascii=False).encode('utf-8')
                status, delay, stall_body, location = owner.status, owner.delay, owner.stall_body, owner.location
                if not stall_body:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    if location:
                        self.send_header('Location', location)
                    self.end_headers()
                    if stall_body:
                        self.wfile.flush()
                        time.sleep(delay)
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/responses'
        self.chat_url = f'http://127.0.0.1:{self.server.server_port}/chat/completions'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def chat_envelope(payload):
    """手写固定教学内容；只随任务取合法步骤，以覆盖四题的传输/校验，不模拟模型效果。"""
    context = json.loads(payload['messages'][1]['content'])['context']
    action = next(iter(context['allowed_next_actions']), None)
    reply = {'schema_version': 1, 'explanation': '本机手写响应：先找单位“1”，再辨认每个分数表示的部分。',
             'next_action': action, 'optional_hint': None,
             'proposed_state_update': {'current_step': action} if action else None,
             'proposed_configuration_update': None,
             'cited_source_ids': [row['source_id'] for row in context['sources'][:1]]}
    return {'id': 'chatcmpl-local-fixture', 'object': 'chat.completion', 'model': 'fixture-chat-v1',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': json.dumps(reply, ensure_ascii=False),
                                     'reasoning_content': 'PRIVATE_REASONING_FIXTURE'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}
