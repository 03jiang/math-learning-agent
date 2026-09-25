"""单次 HTTP 工作进程。密钥经 stdin 传入；不重试，不跟随重定向。"""
from pathlib import Path as _BootstrapPath
import sys as _bootstrap_sys
_bootstrap_sys.path.insert(0,str(_BootstrapPath(__file__).resolve().parents[1]))
import json
import socket
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

OPENAI_ENDPOINT = 'https://api.openai.com/v1/responses'
MAX_REQUEST_BYTES = 131072
MAX_HTTP_BYTES = 262144


def endpoint_kind(url):
    if url in {OPENAI_ENDPOINT, 'https://api.openai.com/v1/chat/completions',
               'https://api.deepseek.com/chat/completions', 'https://api.deepseek.com/v1/chat/completions',
               'https://api.deepseek.com/beta/chat/completions'}:
        return 'real_api'
    parsed = urlsplit(url)
    if (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
            and parsed.path in {'/v1/responses', '/chat/completions', '/v1/chat/completions', '/beta/chat/completions'} and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment):
        return 'local_http_test'
    raise ValueError('不允许的模型服务地址')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def emit(**value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main():
    try:
        envelope = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 8193))
        url, key = envelope['url'], envelope['api_key']
        kind = endpoint_kind(url)
        if (type(key) is not str or not key or len(key) > 4096 or not key.isascii()
                or any(char.isspace() for char in key)
                or (kind == 'local_http_test' and key != 'local-test-key')):
            raise ValueError('无效密钥')
        body = json.dumps(envelope['payload'], ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError('请求过长')
        timeout = envelope['timeout_seconds']
        if type(timeout) not in (int, float) or not 0 < timeout <= 60:
            raise ValueError('无效超时')
        request = Request(url, body, {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
        # 禁用隐式代理，凭证只发往上面限定的端点；HTTPS 使用默认证书验证。
        opener = build_opener(ProxyHandler({}), NoRedirect())
        emit(event='started')
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                emit(event='result', status=status, error_code='response_too_large')
                return
            try:
                decoded = raw.decode('utf-8')
            except UnicodeError:
                emit(event='result', status=status, error_code='invalid_response')
                return
            emit(event='result', status=status, body=decoded)
    except HTTPError as exc:
        # 不输出服务端错误正文、请求头或异常对象，避免凭证/输入回显进入日志。
        emit(event='result', status=exc.code, error_code=f'http_{exc.code}')
        exc.close()
    except (TimeoutError, socket.timeout):
        emit(event='result', error_code='timeout')
    except URLError as exc:
        code = 'tls_error' if isinstance(exc.reason, ssl.SSLError) else 'timeout' if isinstance(exc.reason, TimeoutError) else 'network_error'
        emit(event='result', error_code=code)
    except Exception:
        emit(event='result', error_code='transport_error')


if __name__ == '__main__':
    main()
