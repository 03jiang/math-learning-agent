"""Bounded one-variable linear equation checks; no eval or general CAS.

Only a bare equation (optionally prefixed with 解方程/求解方程/方程) and
whole, unambiguous student lines are checked. Stop at the first unsupported
or non-equivalent step, so an earlier error cannot condemn later local work.
"""
from collections import Counter
from fractions import Fraction
import re

from legacy.step_check import ArithmeticParser, Expression, UnsupportedExpression


def normalize(source):
    return re.sub(r'\s+', '', source.translate(str.maketrans(
        {'×':'*', '÷':'/', '（':'(', '）':')', '−':'-', '／':'/',
         '＋':'+', '－':'-', '＊':'*', '＝':'='}))).rstrip('。')


def pair(node):
    return node.value if isinstance(node.value, tuple) else (Fraction(0), node.value)


class LinearParser(ArithmeticParser):
    def __init__(self, source, symbol):
        self.symbol = symbol
        source = normalize(source)
        if len(source) > 180:
            raise UnsupportedExpression('linear_expression_too_long')
        source = re.sub(r'(\d|\))(?=' + symbol + r'|\()', r'\1*', source)
        source = re.sub(symbol + r'(?=\()', symbol + '*', source)
        self.tokens = re.findall(r'\d+(?:\.\d+)?|[' + symbol + r'()+*/-]', source)
        if (''.join(self.tokens) != source or not self.tokens or len(self.tokens) > 80
                or any(len(token) > 12 for token in self.tokens)):
            raise UnsupportedExpression('unsupported_linear_expression')
        self.index = 0

    def factor(self, depth):
        if depth > 12:
            raise UnsupportedExpression('linear_expression_too_deep')
        token = self.peek()
        if token == self.symbol:
            self.index += 1
            return Expression('variable', (Fraction(1), Fraction(0)))
        if token in ('+', '-'):
            self.index += 1
            node = self.factor(depth + 1)
            return self.combine('*', Expression('number', Fraction(1 if token == '+' else -1)), node)
        return super().factor(depth)

    @staticmethod
    def combine(op, left, right):
        a, b = pair(left); c, d = pair(right)
        if op == '+': result = (a+c, b+d)
        elif op == '-': result = (a-c, b-d)
        elif op == '*':
            if a and c: raise UnsupportedExpression('nonlinear_product')
            result = (a*d+b*c, b*d)
        else:
            if c or not d: raise UnsupportedExpression('variable_or_zero_divisor')
            result = (a/d, b/d)
        if any(abs(v.numerator) > 10**12 or v.denominator > 10**12 for v in result):
            raise UnsupportedExpression('linear_expression_out_of_bounds')
        return Expression(op, result, left, right)


def solution(source, symbol):
    parts = normalize(source).split('=')
    if not 2 <= len(parts) <= 12:
        raise UnsupportedExpression('unsupported_equation')
    terms = [pair(LinearParser(part, symbol).parse()) for part in parts]
    roots = []
    for (a,b), (c,d) in zip(terms, terms[1:]):
        if a == c:
            if b != d: raise UnsupportedExpression('inconsistent_equation_chain')
        else:
            root = (d-b)/(a-c)
            if abs(root.numerator) > 10**12 or root.denominator > 10**12:
                raise UnsupportedExpression('equation_root_out_of_bounds')
            roots.append(root)
    if not roots or any(root != roots[0] for root in roots):
        raise UnsupportedExpression('no_single_equation_solution')
    return roots[0]


def equation_issue(question, student_work, comparisons):
    if not isinstance(question, str) or not isinstance(student_work, str): return None
    reference = re.sub(r'^(?:求解方程|解方程|方程)[:：]?', '', normalize(question))
    symbols = set(re.findall(r'[A-Za-z]', reference))
    if len(symbols) != 1 or not symbols <= {'x','y','z'}: return None
    symbol = next(iter(symbols))
    try: expected = solution(reference, symbol)
    except (ValueError, ZeroDivisionError, RecursionError): return None
    lines = [normalize(line) for line in student_work.splitlines() if line.strip()]
    if not lines or len(lines) > 12: return None
    counts = Counter(lines)
    verified = {}
    for line in lines:
        try: equivalent = solution(line, symbol) == expected
        except (ValueError, ZeroDivisionError, RecursionError): break
        if counts[line] == 1: verified[line] = equivalent
        if not equivalent: break
    for item in comparisons:
        quote = normalize(item['student_excerpt'])
        if item['verdict'] == 'uncertain' or quote not in verified: continue
        if (item['verdict'] == 'correct') != verified[quote]:
            return 'step_equation_verdict_mismatch'
    return None
