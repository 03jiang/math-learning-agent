"""受限分数等式检查。不用 eval，不执行学生代码，不推断概念掌握。"""
from dataclasses import asdict, dataclass
from fractions import Fraction
import re


@dataclass(frozen=True)
class Expression:
    op: str
    value: Fraction
    left: object = None
    right: object = None


class UnsupportedExpression(ValueError):
    pass


class ArithmeticParser:
    def __init__(self, source):
        source = source.translate(str.maketrans({'×': '*', '÷': '/', '（': '(', '）': ')', '−': '-',
                                                '／': '/', '＋': '+', '－': '-', '＊': '*'}))
        source = re.sub(r'\s+', '', source)
        if len(source) > 180:
            raise UnsupportedExpression('算式过长，请拆成较短的等式')
        self.tokens = re.findall(r'\d+(?:\.\d+)?|[()+*/-]', source)
        if ''.join(self.tokens) != source or len(self.tokens) > 80 or not self.tokens:
            raise UnsupportedExpression('暂只支持数字、分数、小数、四则运算和括号')
        if any(len(token) > 12 for token in self.tokens):
            raise UnsupportedExpression('数字过长')
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def factor(self, depth):
        if depth > 12:
            raise UnsupportedExpression('括号或正负号嵌套过深')
        token = self.peek()
        self.index += 1
        if token == '(':
            node = self.addition(depth + 1)
            if self.peek() != ')':
                raise UnsupportedExpression('括号不匹配')
            self.index += 1
            return node
        if token in {'+', '-'}:
            node = self.factor(depth + 1)
            return Expression('sign', node.value if token == '+' else -node.value, node)
        if token is None or not re.fullmatch(r'\d+(?:\.\d+)?', token):
            raise UnsupportedExpression('算式不完整，请检查运算符')
        return Expression('number', Fraction(token))

    @staticmethod
    def combine(op, left, right):
        if op == '+': value = left.value + right.value
        elif op == '-': value = left.value - right.value
        elif op == '*': value = left.value * right.value
        else:
            if right.value == 0:
                raise ZeroDivisionError('分母或除数不能是 0')
            value = left.value / right.value
        if abs(value.numerator) > 10**12 or value.denominator > 10**12:
            raise UnsupportedExpression('计算规模超出本版范围')
        return Expression(op, value, left, right)

    def product(self, depth):
        node = self.factor(depth)
        while self.peek() in {'*', '/'}:
            op = self.peek()
            self.index += 1
            node = self.combine(op, node, self.factor(depth))
        return node

    def addition(self, depth=0):
        node = self.product(depth)
        while self.peek() in {'+', '-'}:
            op = self.peek()
            self.index += 1
            node = self.combine(op, node, self.product(depth))
        return node

    def parse(self):
        result = self.addition()
        if self.index != len(self.tokens):
            raise UnsupportedExpression('不能识别剩余算式，请检查括号和运算符')
        return result


def parse_expression(source):
    return ArithmeticParser(source).parse()


def is_sum(node, left, right):
    return node.op == '+' and sorted([node.left.value, node.right.value]) == sorted([left, right])


def coverage(nodes, left, right, kind):
    found = set()
    def is_number(node):
        return node.op == 'number' or (node.op == '/' and node.left.op == node.right.op == 'number') or (node.op == 'sign' and is_number(node.left))
    numeric_result = any(is_number(node) for node in nodes)
    for node in nodes:
        if is_sum(node, left, right):
            found.add('sum' if numeric_result else 'sum_setup')
        if kind == 'remaining' and node.op == '-':
            if node.left.value == 1 and node.right.value == left + right:
                found.add('remaining' if numeric_result else 'remaining_setup')
                if is_sum(node.right, left, right) and numeric_result:
                    found.add('sum')
            # 同样支持 1 - a - b，而不强制先算总共用去多少。
            first = node.left
            if (first.op == '-' and first.left.value == 1
                    and sorted([first.right.value, node.right.value]) == sorted([left, right])):
                found.update({'sum', 'remaining'} if numeric_result else {'remaining_setup'})
    if len(nodes) > 1 and nodes[0].value in {left, right}:
        found.add('equivalence')
    return found


@dataclass
class StepReport:
    status: str
    rows: list
    coverage: list[str]
    feedback: str
    first_issue: int | None
    verified_scope: str = '只检查支持范围内的等式和本题数量关系；不判断文字解释或概念掌握'

    def to_dict(self):
        return asdict(self)


def check_steps(kind, left, right, submitted):
    if kind not in {'addition', 'remaining'}:
        raise ValueError('未知题型')
    if type(submitted) is not str or len(submitted) > 2000:
        raise ValueError('步骤须为不超过 2000 字符的文本')
    lines = [(i + 1, text.strip()) for i, text in enumerate(submitted.splitlines()) if text.strip()]
    if not lines or len(lines) > 12:
        raise ValueError('请填写 1–12 行步骤，每行一个等式或一句解释')
    rows, covered = [], set()
    for line_number, original in lines:
        row = {'line': line_number, 'submitted': original, 'status': 'unverified', 'category': 'explanation', 'feedback': ''}
        rows.append(row)
        label = ''
        source = original.replace('＝', '=')
        prefix = re.match(r'^(通分|计算|已用|剩余|检查)[:：]\s*', source)
        if prefix:
            label = prefix.group(1)
            source = source[prefix.end():]
        if '=' not in source:
            row['feedback'] = '这句文字解释尚不能自动核对；可以保留给老师看，并补充对应等式。'
            continue
        parts = source.split('=')
        if len(parts) > 5:
            row.update(category='format', feedback='一行最多写 5 个等值式，请拆成多行。')
            continue
        try:
            nodes = [parse_expression(part) for part in parts]
        except UnsupportedExpression as exc:
            row.update(category='format', feedback=str(exc))
            continue
        except ZeroDivisionError as exc:
            row.update(status='incorrect', category='calculation', feedback=str(exc))
            continue
        if any(node.value != nodes[0].value for node in nodes[1:]):
            row.update(status='incorrect', category='calculation',
                       feedback='这行等号两边不相等。请先检查通分是否保持大小不变，再检查运算。')
            continue
        found = coverage(nodes, left, right, kind)
        if kind == 'remaining' and label in {'剩余', '已用'}:
            expected = 1 - left - right if label == '剩余' else left + right
            if nodes[-1].value != expected:
                row.update(status='incorrect', category='quantity', feedback=f'等式数值成立，但它不对应题目中的“{label}”部分。请重新核对所求量。')
                continue
        if label == '检查' and kind == 'remaining':
            # 检查行不能替代求解：只说明两部分之和为 1。
            if any(is_sum(node, left + right, 1 - left - right) for node in nodes):
                found.add('check')
        if found:
            row.update(status='correct', category='calculation', feedback='这行等式成立，可对应本题的计算。')
            covered.update(found)
        else:
            row.update(category='relevance', feedback='这行等式数值成立，但尚不能确认它如何对应本题，请补充计算关系。')
    incorrect = [row for row in rows if row['status'] == 'incorrect']
    unverified = [row for row in rows if row['status'] == 'unverified']
    required = {'sum'} if kind == 'addition' else {'sum', 'remaining'}
    if incorrect:
        status = 'incorrect'
        feedback = f'先检查第 {incorrect[0]["line"]} 行。后面的正确结果不能抵消前面等式中的问题。'
    elif unverified:
        status = 'needs_review'
        feedback = '部分内容尚不能自动核对，请结合逐行提示补充；已核对的等式会单独显示。'
    elif not required <= covered:
        status = 'partial'
        feedback = '已填写的等式成立，但本题计算还不完整。' + ('还需从整体中减去已用部分。' if kind == 'remaining' and 'sum' in covered else '请补上题目所需的计算。')
    else:
        status = 'verified'
        feedback = '所提交的等式和本题所需计算已核对。请继续用自己的话解释每个量的意思。'
    issues = incorrect or unverified
    return StepReport(status, rows, sorted(covered), feedback, issues[0]['line'] if issues else None)
