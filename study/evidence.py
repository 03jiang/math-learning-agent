"""新分析的有限证据检查；不能替代数学/教学审核，也不修改模型原文。"""
import re

from step_check import parse_expression, UnsupportedExpression

EVIDENCE_VERSION = 'analysis-evidence-v1'
ANSWER_ONLY_FEEDBACK = {
    'correct': '当前答案与参考答案相符；仅有答案，不能判断计算过程，请补充步骤。',
    'incorrect': '当前答案与参考答案不符；仅有答案，不能判断计算过程，请补充步骤。',
    'partial': '当前作答尚未完整回答题目要求；仅有答案，不能判断计算过程，请补充步骤。',
    'uncertain': '当前答案还需核对；仅有答案，不能判断计算过程，请补充步骤。',
}

# 仅检测明确归因的常见表述；不是覆盖任意自然语言的语义判定器。
# 参考解法中的“先通分”或补问“请写出计算过程”不属于对学生方法的断言。
METHOD_CLAIM = re.compile(
    r'(?:学生|你|原作答|该答案|这个答案|这一答案)(?:可能|也许|大概|应该|是|的错误是|的错因是)*'
    r'(?:把|将|用了|使用了|采用了|直接|没有通分|没通分|忘了|漏了|误将|误把|混淆了)'
    r'|(?:错误|错因)(?:在于|源于|来自|是因为|是).{0,60}(?:通分|相加|相减|移项|符号|约分)'
    r'|(?:you|the student)\s+(?:added|subtracted|multiplied|divided|forgot|used)\b',
    re.IGNORECASE,
)


def arithmetic_issue(quote, verdict):
    """核对完整数值等式的局部成立性；含变量/文字或超界时不作判断。"""
    source = quote.strip().replace('＝', '=')
    if '=' not in source:
        return None
    if source.startswith('=') or source.endswith('='):
        return 'step_quote_incomplete'
    # 连等式或多条数值等式中，成立与不成立的变形必须分开标注。
    segments = re.split(r'[，,;；\n]+', source)
    relations = []
    for segment in segments:
        parts = segment.strip().rstrip('。').split('=')
        if len(parts) < 2 or len(parts) > 12:
            return None
        try:
            numbers = [parse_expression(part).value for part in parts]
        except (UnsupportedExpression, ZeroDivisionError, ValueError, RecursionError):
            return None
        relations.extend(a == b for a, b in zip(numbers, numbers[1:]))
    if verdict == 'uncertain':
        return None
    if any(relations) and not all(relations):
        return 'step_mixed_equalities'
    if (verdict == 'correct') != all(relations):
        return 'step_arithmetic_verdict_mismatch'
    return None


def evidence_issue(value):
    """结构校验后调用，返回固定错误码；不输出用户/模型正文。"""
    review = value['student_review']
    if review['work_kind'] == 'answer_only':
        if review['answer_feedback'] != ANSWER_ONLY_FEEDBACK.get(review['verdict']):
            return 'answer_only_feedback_not_bounded'
        # 防止把已禁止的过程归因移到摘要、讲解或追问等其他文本中。
        prose = [value[k] for k in ('topic', 'summary', 'answer', 'takeaway', 'next_practice', 'clarification')]
        prose += value['steps'] + value['knowledge_points']
        if any(METHOD_CLAIM.search(s) for s in prose):
            return 'answer_only_method_claim'
    for row in review['comparisons']:
        issue = arithmetic_issue(row['student_excerpt'], row['verdict'])
        if issue:
            return issue
    return None
