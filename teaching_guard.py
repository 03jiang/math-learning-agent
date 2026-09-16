"""四题教学边界：拦截可识别的答案披露、结果归因断言，并约束下一步请求。

这是有限规则，不评估任意中文解释。允许题干中的分数及通分中间量，
只检查加减等式的结果和明确的答案/剩余结论；不修改或补造模型建议。
"""
from fractions import Fraction
import re
import unicodedata

from core import ValidationError


class TeachingContractError(ValidationError):
    def __init__(self, code, reason=None):
        self.code = code
        # reason 仅由下面的固定分支给出，不含被拒正文或学生输入。
        self.details = {'reason': reason} if reason else None
        messages = {
            'hint_answer_revealed': '回复在提示或下一步模式中给出了可识别的最终答案，已拦截；请重新请求提示或选择完整讲解',
            'missing_next_proposal': '回复缺少本次要求的下一步建议，已拦截；请重新请求，本轮不自动重试',
            'unsupported_method_claim': '回复仅凭答案断定了解题方法，已拦截；请补充计算步骤，本轮不自动重试',
        }
        super().__init__(messages[code])


NUMBER = r'(?:\d{1,9}\s*/\s*\d{1,9}|\d{1,9}(?:\.\d{1,9})?|[一二三四五六七八九十]{1,3}分之[一二三四五六七八九十]{1,3})'
END = r'(?![\d./]|\s*/|\s*[+×÷*-])'
EQUATION = re.compile(r'(?P<left>[\d\s./()+-]{1,120})(?<![=!<>])=\s*(?P<value>' + NUMBER + ')' + END)
CONCLUSION = re.compile(
    r'(?:最终答案|答案|结果)(?:应该|应当)?(?:就是|等于|为|是|[:=])\s*(?P<value>' + NUMBER + ')' + END)
REMAINING = re.compile(
    r'(?:还剩|剩余|剩下)(?:的部分)?(?:全长|整个蛋糕|原来整体)?(?:的|就是|等于|为|是)?\s*(?P<value>' + NUMBER + ')' + END)

# 只约束已观察到的“你的结果说明分母相加”式因果断言。
# 不判断任意中文方法归因；直接讨论学生写出的算式不属于此模式。
METHOD_FROM_RESULT = re.compile(
    r'(?:你的?|你提交的|这个|该)(?:答案|结果).{0,48}(?:说明|表明|证明)'
    r'.{0,60}(?:分子|分母).{0,40}(?:相加|加在一起|加起来)')
QUALIFIED_METHOD = re.compile(r'可能|也许|或许|未必|不一定|(?:不能|并不|无法)(?:说明|表明|证明)')


def claims_method_from_result(text):
    for sentence in re.findall(r'[^。！？!?；;\n]+[。！？!?；;\n]?', normalize(text)):
        if (METHOD_FROM_RESULT.search(sentence) and not QUALIFIED_METHOD.search(sentence)
                and not sentence.rstrip().endswith(('?', '？'))):
            return True
    return False


def normalize(text):
    text = unicodedata.normalize('NFKC', text).replace('−', '-').replace('／', '/')
    text = re.sub(r'\\(?:d?frac)\s*\{(\d{1,9})\}\s*\{(\d{1,9})\}', r'\1/\2', text)
    return re.sub(r'[`$*“”‘’]', '', text)


def chinese_integer(token):
    digits = {char: value for value, char in enumerate('零一二三四五六七八九')}
    if '十' in token:
        tens, units = token.split('十')
        return (digits[tens] if tens else 1) * 10 + (digits[units] if units else 0)
    return digits[token]


def numeric_value(token):
    try:
        if '分之' in token:
            denominator, numerator = token.split('分之')
            return Fraction(chinese_integer(numerator), chinese_integer(denominator))
        return Fraction(re.sub(r'\s+', '', token))
    except (ValueError, ZeroDivisionError, KeyError):
        return None


def reveals_answer(text, reference):
    text = normalize(text)
    for match in EQUATION.finditer(text):
        # 单纯 1/4 = 2/8 是通分，不因碰巧等于本题答案就拦截。
        if re.search(r'\d\s*\)?\s*[+-]', match['left']) and numeric_value(match['value']) == reference:
            return True
    return any(numeric_value(match['value']) == reference
               for pattern in (CONCLUSION, REMAINING) for match in pattern.finditer(text))


def validate_teaching_contract(data, context):
    action = data['next_action']
    if context.get('requested_help') == 'next' and context['allowed_next_actions']:
        # 原字段校验先验证白名单及 current_step 一致性；此处禁止空建议。
        if action is None:
            raise TeachingContractError('missing_next_proposal', 'missing_next_action')
        if action != context['task']['current_step'] and data['proposed_state_update'] is None:
            raise TeachingContractError('missing_next_proposal', 'missing_state_update')
    if any(claims_method_from_result(data[field] or '') for field in ('explanation', 'optional_hint')):
        raise TeachingContractError('unsupported_method_claim', 'answer_is_not_method_evidence')
    hint = context['effective_settings']['explanation_mode'] == 'hint' or context.get('requested_help') == 'next'
    submitted = context['local_checks']['answer']
    # 学生已提交正确数值时，可以确认该数值；不因此推断过程正确或掌握。
    if hint and not (submitted and submitted['status'] == 'correct'):
        reference = Fraction(context['reference_answer'])
        if any(reveals_answer(data[field] or '', reference) for field in ('explanation', 'optional_hint')):
            raise TeachingContractError('hint_answer_revealed')
