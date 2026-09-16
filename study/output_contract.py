"""可选的严格返回格式：单次强制函数参数承载结果，不执行工具或发起循环。"""
from copy import deepcopy
import json
import re

from model_api import ModelAPIError
from study.diagnosis import WORK_KINDS, VERDICTS, validate_analysis
from study.notebook import REASONS

OUTPUT_MODES = ('json_object', 'strict_tool')
STRICT_ENDPOINT = 'https://api.deepseek.com/beta/chat/completions'
CONTRACT_VERSION = 'study-strict-output-v2'
FUNCTIONS = {'analyze': 'return_math_analysis', 'reanalyze': 'return_math_correction',
             'recognize': 'return_math_transcription'}
ISSUES = {
    'duplicate_json_key': '模型回复包含重复 JSON 字段，即使值相同也不自动合并。',
    'non_standard_json': '模型回复包含非标准 JSON 数值。',
    'invalid_json': '模型回复不是完整有效的 JSON。',
    'reply_too_large': '模型回复过长或编码无效。',
}


class OutputParseError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(ISSUES[code])


def parse_output(raw):
    if type(raw) is not str:
        raise OutputParseError('invalid_json')
    try:
        if len(raw.encode('utf-8')) > 32000:
            raise OutputParseError('reply_too_large')
    except UnicodeError:
        raise OutputParseError('reply_too_large') from None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise OutputParseError('duplicate_json_key')
            result[key] = value
        return result

    def no_constant(_):
        raise OutputParseError('non_standard_json')

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=no_constant)
    except (json.JSONDecodeError, RecursionError):
        raise OutputParseError('invalid_json') from None


def endpoint(config, output_mode):
    if output_mode not in OUTPUT_MODES:
        raise ValueError('未知返回格式。')
    if output_mode == 'strict_tool':
        if config.thinking != 'disabled':
            raise ValueError('strict 返回格式要求关闭思考模式；不会自动替换配置。')
        return STRICT_ENDPOINT
    return config.base_url + '/chat/completions'


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def string(values=None):
    return {'type': 'string', **({'enum': list(values)} if values is not None else {})}


def array(items):
    return {'type': 'array', 'items': items}


def change_schema(statuses, previous_excerpts=None):
    return obj({'previous_excerpt': string(previous_excerpts), 'current_excerpt': string(),
                'status': string(statuses), 'explanation': string()})


def correction_change_schema(context):
    """已订正/仍错误只可引用旧分析明确判错的整条引用；其他变化不作纠错声明。"""
    before = context.get('previous_analysis') or {}
    if before:
        validate_analysis(before, student_work=context['previous_student_work'], allow_legacy=True)
    comparable = (before.get('schema_version') == 2 and before['status'] == 'solved'
                  and before['student_review']['work_kind'] == 'steps'
                  and context['student_work_kind'] == 'steps')
    wrong = list(dict.fromkeys(row['student_excerpt'] for row in before['student_review']['comparisons']
                              if row['verdict'] == 'incorrect')) if comparable else []
    neutral = change_schema(('changed', 'uncertain'))
    # 不生成空 enum，也不从最终答案不符推断每个旧步骤均错误。
    if not wrong:
        return neutral
    return {'anyOf': [change_schema(('corrected', 'still_incorrect'), wrong), neutral]}


def output_schema(operation, *, context=None):
    # Beta 使用官方列出的基础类型、enum 与 anyOf。新步骤语义仍由应用检查。
    if operation == 'recognize':
        return obj({'text': string(), 'student_work': string(),
                    'work_kind': string(WORK_KINDS), 'warnings': array(string())})
    analysis = obj({
        'schema_version': {'type': 'integer', 'enum': [2]},
        'status': string(('solved', 'needs_clarification')), 'topic': string(),
        'summary': string(), 'steps': array(string()), 'answer': string(),
        'student_review': obj({
            'work_kind': string(WORK_KINDS), 'verdict': string(VERDICTS),
            'observed_approach': string(), 'answer_feedback': string(),
            'comparisons': array(obj({'student_excerpt': string(), 'reference_step': string(),
                'verdict': string(('correct', 'incorrect', 'uncertain')), 'explanation': string()})),
        }),
        'knowledge_points': array(string()),
        'diagnosis': array(obj({
            'category': string(REASONS[1:]), 'knowledge_point': string(),
            'evidence': string(), 'explanation': string(), 'check_question': string(),
        })),
        'takeaway': string(), 'next_practice': string(), 'clarification': string(),
    })
    if operation == 'analyze':
        return analysis
    if operation == 'reanalyze':
        changes = (correction_change_schema(context) if context is not None else
                   change_schema(('corrected', 'still_incorrect', 'changed', 'uncertain')))
        return obj({'schema_version': {'type': 'integer', 'enum': [1]}, 'analysis': analysis,
            'comparison': obj({'summary': string(), 'changes': array(changes)})})
    raise ValueError('未知结构化输出操作。')


def strict_payload(payload, operation, *, context=None):
    if operation == 'reanalyze' and context is None:
        raise ValueError('严格订正格式必须提供已核对的前后作答上下文。')
    schema = output_schema(operation, context=context)
    name = FUNCTIONS[operation]
    result = deepcopy(payload)
    result.pop('response_format', None)
    result['messages'][0]['content'] += (
        '\n本次结果只放入指定函数 ' + name + ' 的 arguments，不在 content 中重复回答。'
        '函数仅承载待核对的结果；它不会执行工具、保存记录或改变状态。所有字段只出现一次。')
    result['tools'] = [{'type': 'function', 'function': {
        'name': name, 'description': 'Return the requested study result for validation. Does not save or execute actions.',
        'strict': True, 'parameters': schema,
    }}]
    result['tool_choice'] = {'type': 'function', 'function': {'name': name}}
    return result


def strict_response_text(data, record, operation):
    """仅允许一个指定的函数调用；不执行任何函数、不发送工具结果或第二轮请求。"""
    if type(data) is not dict or data.get('object') != 'chat.completion':
        raise ModelAPIError('invalid_response')
    usage = data.get('usage')
    if type(usage) is dict:
        values = [usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if all(type(v) is int and v >= 0 for v in values) and sum(values[:2]) == values[2]:
            record['usage'] = dict(zip(('input_tokens', 'output_tokens', 'total_tokens'), values))
    model = data.get('model')
    if type(model) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', model):
        record['response_model'] = model
    choices = data.get('choices')
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise ModelAPIError('invalid_response')
    choice = choices[0]
    finish = choice.get('finish_reason')
    if finish in ('length', 'aborted', 'insufficient_system_resource'):
        raise ModelAPIError('incomplete')
    if finish == 'content_filter':
        raise ModelAPIError('refusal')
    message = choice.get('message')
    if type(message) is not dict or message.get('role') != 'assistant':
        raise ModelAPIError('invalid_response')
    if message.get('refusal'):
        raise ModelAPIError('refusal')
    if (finish != 'tool_calls' or message.get('function_call')
            or message.get('content') not in (None, '')):
        raise ModelAPIError('invalid_response')
    calls = message.get('tool_calls')
    if type(calls) is not list or len(calls) != 1 or type(calls[0]) is not dict:
        raise ModelAPIError('invalid_response')
    call = calls[0]
    call_id = call.get('id')
    if (call.get('type') != 'function' or type(call_id) is not str or not call_id.strip()
            or len(call_id) > 256 or not call_id.isprintable()):
        raise ModelAPIError('invalid_response')
    function = call.get('function')
    if (type(function) is not dict or set(function) != {'name', 'arguments'}
            or function['name'] != FUNCTIONS.get(operation)):
        raise ModelAPIError('invalid_response')
    arguments = function['arguments']
    if type(arguments) is not str or not arguments.strip():
        raise OutputParseError('invalid_json')
    record['output_function'] = function['name']
    record['output_call_id'] = call_id
    return arguments
