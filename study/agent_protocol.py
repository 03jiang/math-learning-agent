"""Agent 专用 Chat Completions 分流；不放宽旧文本协议。"""
import re

from model_api import chat_response_text
from study.output_contract import parse_output

VERSION = 'study-agent-v1'


class AgentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('资料辅助分析已停止（' + code + '）；未自动重试或收藏。')


def parse_turn(envelope, record, seen_ids):
    if type(envelope) is not dict or envelope.get('object') != 'chat.completion':
        raise AgentError('invalid_envelope')
    # 为工具回合也保留实际模型与用量；缺失或异常保持未知。
    usage = envelope.get('usage')
    if type(usage) is dict:
        counts = [usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if all(type(n) is int and n >= 0 for n in counts) and counts[0] + counts[1] == counts[2]:
            record['usage'] = dict(zip(('input_tokens','output_tokens','total_tokens'), counts))
    model = envelope.get('model')
    if type(model) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
        record['response_model'] = model
    choices = envelope.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise AgentError('invalid_choices')
    choice = choices[0]; message = choice.get('message')
    if type(message) is dict and type(message.get('tool_calls')) is list:
        record['tool_requests_received']=len(message['tool_calls'])
    if type(message) is not dict or message.get('role') != 'assistant' or message.get('function_call') or message.get('refusal'):
        raise AgentError('invalid_message')
    if choice.get('finish_reason') == 'stop':
        # 旧解析器继续拒绝工具请求；这里只对确认是最终正文的分支复用它。
        return {'kind':'final', 'value':parse_output(chat_response_text(envelope, record))}
    calls = message.get('tool_calls')
    if (choice.get('finish_reason') != 'tool_calls' or message.get('content') not in (None, '')
            or type(calls) is not list or not 1 <= len(calls) <= 16):
        raise AgentError('invalid_tool_turn')
    result=[]; current=set()
    for call in calls:
        if type(call) is not dict or set(call) != {'id','type','function'} or call['type'] != 'function':
            raise AgentError('invalid_tool_call')
        call_id=call['id']; function=call['function']
        if type(call_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',call_id):
            raise AgentError('invalid_call_id')
        if call_id in seen_ids or call_id in current: raise AgentError('duplicate_call_id')
        current.add(call_id)
        if type(function) is not dict or set(function) != {'name','arguments'}:
            raise AgentError('invalid_tool_function')
        if type(function['name']) is not str or not re.fullmatch(r'[a-z_]{1,64}',function['name']):
            raise AgentError('invalid_tool_name')
        if type(function['arguments']) is not str or len(function['arguments'])>2000:
            raise AgentError('invalid_arguments')
        arguments=parse_output(function['arguments'])
        if type(arguments) is not dict: raise AgentError('invalid_arguments')
        result.append({'id':call_id,'name':function['name'],'arguments':arguments})
    return {'kind':'tools','calls':result,'message':{'role':'assistant','content':None,'tool_calls':[
        {'id':c['id'],'type':'function','function':{'name':c['name'],'arguments':calls[i]['function']['arguments']}}
        for i,c in enumerate(result)]}}


def result_message(call_id, result):
    import json
    return {'role':'tool','tool_call_id':call_id,'content':json.dumps(result,ensure_ascii=False)}


def validate_final(value, sources, validate_result):
    if type(value) is not dict or set(value) != {'result','citations'}:
        raise AgentError('invalid_final_fields')
    validate_result(value['result'])
    citations=value['citations']
    if type(citations) is not list or len(citations)>9: raise AgentError('invalid_citations')
    seen=set()
    for row in citations:
        if type(row) is not dict or set(row) != {'source_id','quote'}: raise AgentError('invalid_citation')
        source_id=row['source_id']; quote=row['quote']
        if type(source_id) is not str or source_id not in sources or source_id in seen:
            raise AgentError('unknown_or_duplicate_source')
        if type(quote) is not str or not quote.strip() or len(quote)>600 or quote not in sources[source_id]['snippet']:
            raise AgentError('unsupported_source_quote')
        seen.add(source_id)
    return value
