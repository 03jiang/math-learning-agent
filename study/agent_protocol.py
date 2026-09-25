"""Agent 专用 Chat Completions 分流；不放宽旧文本协议。"""
import re
from copy import deepcopy

from legacy.model_api import chat_response_text
from study.output_contract import parse_output

VERSION = 'study-agent-v6'


def normalize_final(value, operation):
    """兼容已观察到的空扩展备注；保留原对象，绝不修改教学文本或其他字段。"""
    candidate=deepcopy(value);changes=[]
    result=candidate.get('result') if type(candidate) is dict else None
    analysis=(result.get('analysis') if type(result) is dict else None) if operation=='reanalyze' else result
    review=analysis.get('student_review') if type(analysis) is dict else None
    if (operation in ('analyze','reanalyze') and type(review) is dict
            and type(review.get('answer_feedback_note')) is str and review['answer_feedback_note']==''):
        del review['answer_feedback_note']
        path='result.'+('analysis.' if operation=='reanalyze' else '')+'student_review.answer_feedback_note'
        changes.append({'path':path,'original_value':'','action':'remove_empty_extension'})
    return candidate,{'policy':'empty-review-note-v1','changes':changes}


class AgentError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('资料辅助分析已停止（' + code + '）；未自动重试或收藏。')


def tool_response_diagnostic(choice):
    """限长白名单诊断，不保存未知字段值或 reasoning_content；不能用于执行。"""
    def field(value, limit):
        result = {'type': type(value).__name__}
        if type(value) is str:
            result.update(value=value[:limit], truncated=len(value) > limit)
        elif value is None or type(value) in (bool, int):
            result['value'] = value
        return result

    def shape(value):
        result = {'type': type(value).__name__}
        if type(value) is dict:
            result.update(keys=[k[:64] for k in list(value)[:32]], key_count=len(value))
        return result

    message = choice.get('message')
    if type(message) is not dict or 'tool_calls' not in message:
        return None
    calls = message['tool_calls']
    diagnostic = {'finish_reason': field(choice.get('finish_reason'), 64),
                  'tool_calls_type': type(calls).__name__}
    if type(calls) is not list:
        return diagnostic
    diagnostic.update(tool_call_count=len(calls), truncated=len(calls) > 16, calls=[])
    for call in calls[:16]:
        item = shape(call)
        if type(call) is dict:
            for name, limit in (('id', 128), ('type', 64), ('index', 16)):
                if name in call:
                    item[name] = field(call[name], limit)
            if 'function' in call:
                function = call['function']; item['function'] = shape(function)
                if type(function) is dict:
                    for name, limit in (('name', 64), ('arguments', 2000)):
                        if name in function:
                            item['function'][name] = field(function[name], limit)
        diagnostic['calls'].append(item)
    return diagnostic


def parse_turn(envelope, record, seen_ids, *, final_function=None):
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
    diagnostic = tool_response_diagnostic(choice)
    if diagnostic is not None:
        record['tool_response_diagnostic'] = diagnostic
    if type(message) is dict and type(message.get('tool_calls')) is list:
        record['tool_requests_received']=len(message['tool_calls'])
    if type(message) is not dict or message.get('role') != 'assistant' or message.get('function_call') or message.get('refusal'):
        raise AgentError('invalid_message')
    if choice.get('finish_reason') == 'stop':
        if final_function is not None:
            raise AgentError('final_output_function_required')
        # 旧解析器继续拒绝工具请求；这里只对确认是最终正文的分支复用它。
        return {'kind':'final', 'value':parse_output(chat_response_text(envelope, record))}
    calls = message.get('tool_calls')
    if (choice.get('finish_reason') != 'tool_calls' or message.get('content') not in (None, '')
            or type(calls) is not list or not 1 <= len(calls) <= 16):
        raise AgentError('invalid_tool_turn')
    result=[]; current=set()
    for position, call in enumerate(calls):
        required = {'id', 'type', 'function'}
        if (type(call) is not dict or not required <= set(call)
                or set(call) - required - {'index'} or call['type'] != 'function'):
            raise AgentError('invalid_tool_call')
        # DeepSeek 的官方非流式示例也含 index。仅接受与数组位置一致的整数，
        # 归一化后不回传该元数据；不接受流式分片或其他额外字段。
        if 'index' in call and (type(call['index']) is not int or call['index'] != position):
            raise AgentError('invalid_call_index')
        call_id=call['id']; function=call['function']
        if type(call_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',call_id):
            raise AgentError('invalid_call_id')
        if call_id in seen_ids or call_id in current: raise AgentError('duplicate_call_id')
        current.add(call_id)
        if type(function) is not dict or set(function) != {'name','arguments'}:
            raise AgentError('invalid_tool_function')
        if type(function['name']) is not str or not re.fullmatch(r'[a-z_]{1,64}',function['name']):
            raise AgentError('invalid_tool_name')
        is_final=(final_function is not None and function['name']==final_function)
        if type(function['arguments']) is not str or (not is_final and len(function['arguments'])>2000):
            raise AgentError('invalid_arguments')
        # 最终结果沿用正文的 32000 字节限制，资料查询参数仍限 2000 字符。
        arguments=parse_output(function['arguments'])
        if type(arguments) is not dict: raise AgentError('invalid_arguments')
        result.append({'id':call_id,'name':function['name'],'arguments':arguments})
    if final_function is not None and any(c['name'] == final_function for c in result):
        if len(result) != 1:
            raise AgentError('mixed_final_and_read_tools')
        # 结果载体不是可执行工具；同样占一次模型请求，不能进入 ToolScope。
        record.update(tool_requests_received=0, output_function=final_function,
                      output_call_id=result[0]['id'])
        return {'kind': 'final', 'value': result[0]['arguments']}
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
