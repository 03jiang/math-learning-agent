"""DeepSeek Chat Completions / OpenAI Responses 接入、配置和单次调用记录；默认关闭真实请求。"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from legacy.core import TutorReply, ValidationError
from legacy.curriculum import MockTutor
from legacy.http_worker import MAX_REQUEST_BYTES, OPENAI_ENDPOINT, endpoint_kind
from legacy.model_boundary import (build_model_request, parse_model_response,
                            _reject_constant, _unique_object)
from legacy.teaching_guard import TeachingContractError

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ModelConfig:
    mode: str = 'mock'
    provider: str = 'openai'
    model: str = ''
    timeout_seconds: int = 30
    max_output_tokens: int = 2000
    api_format: str = 'responses'
    base_url: str = 'https://api.openai.com/v1'
    temperature: float = 0.2
    thinking: str = 'disabled'

    def __post_init__(self):
        if self.mode not in ('mock', 'api') or self.provider not in ('openai', 'deepseek'):
            raise ValidationError('mode 只能为 mock/api；provider 只支持 openai/deepseek')
        if self.api_format not in ('responses', 'chat_completions'):
            raise ValidationError('api_format 只能为 responses/chat_completions')
        allowed = {'openai': {'https://api.openai.com/v1'},
                   'deepseek': {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}}
        if type(self.base_url) is not str or self.base_url not in allowed[self.provider]:
            raise ValidationError('base_url 必须为对应服务商的官方 HTTPS 地址（不含端点或尾斜杠）')
        if self.provider == 'deepseek' and self.api_format != 'chat_completions':
            raise ValidationError('本项目的 DeepSeek 接入使用 chat_completions')
        if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 2:
            raise ValidationError('temperature 必须为 0 至 2 的数值')
        if self.thinking not in ('disabled', 'enabled'):
            raise ValidationError('thinking 只能为 disabled/enabled')
        if type(self.model) is not str or (self.model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', self.model)):
            raise ValidationError('模型标识格式不合法')
        if self.mode == 'api' and not self.model:
            raise ValidationError('API 模式必须明确填写 model，程序不自动选择模型')
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise ValidationError('timeout_seconds 必须为 1 至 60 的整数')
        if type(self.max_output_tokens) is not int or not 256 <= self.max_output_tokens <= 8192:
            raise ValidationError('max_output_tokens 必须为 256 至 8192 的整数')


def load_model_config(path=None):
    explicit = path is not None or bool(os.environ.get('MATH_MODEL_CONFIG'))
    selected = Path(path or os.environ.get('MATH_MODEL_CONFIG') or ROOT / 'model_config.json')
    if not selected.exists():
        if explicit:
            raise ValidationError('指定的模型配置文件不存在')
        return ModelConfig()
    try:
        with selected.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValidationError('模型配置文件超过 8 KiB')
        data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        legacy = {'mode', 'provider', 'model', 'timeout_seconds', 'max_output_tokens'}
        if type(data) is not dict or set(data) not in (legacy, set(asdict(ModelConfig()))):
            raise ValidationError('模型配置字段必须与示例一致；密钥不能写入该文件')
        return ModelConfig(**data)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError('模型配置不是合法 JSON') from None


class ModelAPIError(ValueError):
    def __init__(self, code):
        self.code = code
        messages = {'missing_key': '缺少模型 API 密钥（OPENAI_API_KEY 或 DEEPSEEK_API_KEY），请在本机终端安全输入',
                    'timeout': '模型请求超时，工作进程已结束；本轮不会自动重试',
                    'network_error': '无法连接模型服务，本轮不会自动重试',
                    'tls_error': '模型服务证书验证失败',
                    'http_401': '模型服务拒绝认证，请检查本机密钥',
                    'http_403': '当前账号没有访问权限',
                    'http_404': '模型或接口不存在，请检查 model',
                    'http_429': '模型服务限额或请求频率受限，本轮不会自动重试',
                    'incomplete': '模型回复未完整生成，本轮没有更新建议',
                    'refusal': '模型拒绝了本次请求，本轮没有更新建议',
                    'invalid_response': '模型服务返回了不支持的响应结构',
                    'invalid_content': '模型回复未通过本地字段、来源或更新校验',
                    'request_too_large': '当前任务上下文超过 128 KiB，未发送请求',
                    'response_too_large': '模型响应超过 256 KiB 限制',
                    'transport_error': '模型请求进程失败，本轮不会自动重试'}
        messages['credential_echo'] = '模型响应意外包含凭证内容，已拒绝展示和保存'
        for teaching_code in ('hint_answer_revealed', 'missing_next_proposal', 'unsupported_method_claim'):
            messages[teaching_code] = str(TeachingContractError(teaching_code))
        super().__init__(messages.get(code, f'模型请求失败（{code}），请检查服务或配置'))


def _events(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    result = []
    for line in (raw or '').splitlines():
        try:
            item = json.loads(line)
            if type(item) is dict:
                result.append(item)
        except ValueError:
            continue
    return result


class HttpTransport:
    """生产端点固定；仅测试代码可注入 127.0.0.1 和固定测试凭证。"""
    def __init__(self, endpoint=OPENAI_ENDPOINT):
        self.endpoint = endpoint
        self.kind = endpoint_kind(endpoint)

    def send(self, payload, key, timeout_seconds, record):
        if self.kind == 'local_http_test' and key != 'local-test-key':
            raise ModelAPIError('transport_error')
        if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            raise ModelAPIError('request_too_large')
        envelope = {'url': self.endpoint, 'api_key': key, 'payload': payload, 'timeout_seconds': timeout_seconds}
        try:
            process = subprocess.run([sys.executable, str(ROOT / 'legacy/http_worker.py')],
                                     input=json.dumps(envelope, ensure_ascii=False), capture_output=True,
                                     text=True, encoding='utf-8', timeout=timeout_seconds)
            events = _events(process.stdout)
        except subprocess.TimeoutExpired as exc:
            events = _events(exc.stdout)
            record['attempted_requests'] = int(any(item.get('event') == 'started' for item in events))
            raise ModelAPIError('timeout') from None
        except OSError:
            raise ModelAPIError('transport_error') from None
        record['attempted_requests'] = int(any(item.get('event') == 'started' for item in events))
        completed = [item for item in events if item.get('event') == 'result']
        if process.returncode or len(completed) != 1:
            raise ModelAPIError('transport_error')
        result = completed[0]
        record['http_status'] = result.get('status')
        if result.get('error_code'):
            raise ModelAPIError(result['error_code'])
        if result.get('status') != 200 or type(result.get('body')) is not str:
            raise ModelAPIError('invalid_response')
        try:
            return json.loads(result['body'], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise ModelAPIError('invalid_response') from None


def response_text(data, record):
    if type(data) is not dict:
        raise ModelAPIError('invalid_response')
    # 只记录明确存在的计数；缺失或异常不是 0，费用不靠猜测。
    usage = data.get('usage')
    keys = ('input_tokens', 'output_tokens', 'total_tokens')
    if type(usage) is dict and all(type(usage.get(key)) is int and usage[key] >= 0 for key in keys):
        if usage['input_tokens'] + usage['output_tokens'] == usage['total_tokens']:
            record['usage'] = {key: usage[key] for key in keys}
    model = data.get('model')
    if type(model) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
        record['response_model'] = model
    if data.get('status') != 'completed':
        raise ModelAPIError('incomplete' if data.get('status') == 'incomplete' else 'invalid_response')
    output = data.get('output')
    if type(output) is not list or not output or len(output) > 32:
        raise ModelAPIError('invalid_response')
    texts = []
    for item in output:
        if type(item) is not dict:
            raise ModelAPIError('invalid_response')
        if item.get('type') == 'reasoning':
            continue  # 不展示或记录推理内容
        if item.get('type') != 'message' or item.get('role') != 'assistant' or item.get('status') != 'completed':
            raise ModelAPIError('invalid_response')
        content = item.get('content')
        if type(content) is not list or not content:
            raise ModelAPIError('invalid_response')
        for part in content:
            if type(part) is not dict:
                raise ModelAPIError('invalid_response')
            if part.get('type') == 'refusal':
                raise ModelAPIError('refusal')
            if part.get('type') != 'output_text' or type(part.get('text')) is not str:
                raise ModelAPIError('invalid_response')
            texts.append(part['text'])
    if not texts:
        raise ModelAPIError('invalid_response')
    return ''.join(texts)


def chat_response_text(data, record):
    """Chat Completions 只读取最终 content；忽略 reasoning_content，不保存推理。"""
    if type(data) is not dict or data.get('object') != 'chat.completion':
        raise ModelAPIError('invalid_response')
    usage = data.get('usage')
    if type(usage) is dict:
        values = [usage.get(key) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if all(type(value) is int and value >= 0 for value in values) and sum(values[:2]) == values[2]:
            record['usage'] = dict(zip(('input_tokens', 'output_tokens', 'total_tokens'), values))
    model = data.get('model')
    if type(model) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
        record['response_model'] = model
    choices = data.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ModelAPIError('invalid_response')
    choice = choices[0]
    finish = choice.get('finish_reason')
    if finish != 'stop':
        raise ModelAPIError('incomplete' if finish in ('length', 'aborted', 'insufficient_system_resource')
                            else 'refusal' if finish == 'content_filter' else 'invalid_response')
    message = choice.get('message')
    if type(message) is not dict or message.get('role') != 'assistant' or message.get('tool_calls') or message.get('function_call'):
        raise ModelAPIError('invalid_response')
    if message.get('refusal'):
        raise ModelAPIError('refusal')
    content = message.get('content')
    if type(content) is not str or not content.strip():
        raise ModelAPIError('invalid_content')
    return content


def api_key_variable(config):
    return 'DEEPSEEK_API_KEY' if config.provider == 'deepseek' else 'OPENAI_API_KEY'


def request_payload(config, request):
    context = json.dumps({key: value for key, value in request.items() if key != 'instructions'}, ensure_ascii=False)
    if config.api_format == 'chat_completions':
        payload = {'model': config.model, 'messages': [{'role': 'system', 'content': request['instructions']},
                                                      {'role': 'user', 'content': context}],
                   'response_format': {'type': 'json_object'}, 'max_tokens': config.max_output_tokens,
                   'temperature': config.temperature, 'stream': False}
        if config.provider == 'deepseek':
            payload['thinking'] = {'type': config.thinking}
        return payload
    return {'model': config.model, 'instructions': request['instructions'], 'input': context,
            'text': {'format': {'type': 'json_object'}}, 'max_output_tokens': config.max_output_tokens,
            'store': False, 'stream': False}


class ApiTutor:
    uses_http = True

    def __init__(self, config, api_key, *, transport=None, prompt_variant='protocol'):
        if config.mode != 'api':
            raise ValidationError('ApiTutor 需要明确的 api 模式')
        if (type(api_key) is not str or not api_key or len(api_key) > 4096
                or not api_key.isascii() or any(char.isspace() for char in api_key)):
            raise ModelAPIError('missing_key')
        self.config = config
        self._api_key = api_key
        endpoint = config.base_url + ('/responses' if config.api_format == 'responses' else '/chat/completions')
        self.transport = transport or HttpTransport(endpoint)
        if prompt_variant not in ('baseline', 'protocol'):
            raise ValidationError('未知提示词版本')
        self.prompt_variant = prompt_variant
        self.name = f'{config.provider}/{config.model}'
        self.call_records = []

    def plan(self, message):
        return MockTutor().plan(message)

    def answer(self, message, snapshot, settings, sources, searched, *, answer_submission=None, solution_steps='', reading_selections=None, help_action=None):
        request = build_model_request(message, snapshot, settings, sources,
                                      answer_submission=answer_submission, solution_steps=solution_steps,
                                      reading_selections=reading_selections, prompt_variant=self.prompt_variant, help_action=help_action)
        payload = request_payload(self.config, request)
        record = {'request_hash': hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                  'kind': self.transport.kind, 'provider': self.config.provider,
                  'requested_model': self.config.model, 'response_model': None, 'prompt_version': request['prompt_version'],
                  'attempted_requests': 0, 'http_status': None, 'status': 'error', 'error_code': None,
                  'validation_details': None,
                  'usage': None, 'cost': None, 'completion_unknown': False,
                  'parameters': {'timeout_seconds': self.config.timeout_seconds,
                                 'max_output_tokens': self.config.max_output_tokens, 'format': 'json_object',
                                 'api_format': self.config.api_format, 'base_url': self.config.base_url,
                                 'temperature': self.config.temperature if self.config.api_format == 'chat_completions' else None,
                                 'thinking': self.config.thinking if self.config.provider == 'deepseek' else None}}
        self.call_records.append(record)
        start = time.monotonic()
        try:
            envelope = self.transport.send(payload, self._api_key, self.config.timeout_seconds, record)
            if self._api_key in json.dumps(envelope, ensure_ascii=False):
                raise ModelAPIError('credential_echo')
            raw = (chat_response_text if self.config.api_format == 'chat_completions' else response_text)(envelope, record)
            try:
                parsed = parse_model_response(raw, request)
            except TeachingContractError as exc:
                record['validation_details'] = deepcopy(exc.details)
                raise ModelAPIError(exc.code) from None
            except (ValueError, UnicodeError):
                raise ModelAPIError('invalid_content') from None
            record['status'] = 'ok'
            checks = request['context']['local_checks']
            label = f'真实模型 · {self.config.provider}' if self.transport.kind == 'real_api' else '本地 HTTP 接口测试 · 无真实模型'
            return TutorReply(parsed['explanation'], parsed['next_action'] or '先核对输入，或说明还需要哪种帮助。',
                              parsed['optional_hint'], parsed['proposed_state_update'], parsed['proposed_configuration_update'],
                              f'{label} · {self.config.model}', deepcopy(checks['answer']), deepcopy(checks['steps']),
                              parsed['cited_source_ids'], self.transport.kind)
        except ModelAPIError as exc:
            record['error_code'] = exc.code
            record['completion_unknown'] = bool(record['attempted_requests'] and record['http_status'] is None)
            raise
        finally:
            record['elapsed_ms'] = round((time.monotonic() - start) * 1000, 2)


def configured_tutor(config=None):
    config = config or load_model_config()
    if config.mode == 'mock':
        return MockTutor()  # 即使环境中有密钥，也不会自行启用 API
    return ApiTutor(config, os.environ.get(api_key_variable(config), ''))
