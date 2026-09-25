from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from legacy.curriculum import ANSWERS, OPERANDS, check_answer, check_solution
from legacy.step_check import UnsupportedExpression, parse_expression
from legacy.workflow import Workspace


class SolutionStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)

    def test_exact_arithmetic_and_precedence(self):
        for expression, expected in [('1/2+1/4', Fraction(3,4)), ('1-(1/2+1/4)', Fraction(1,4)),
                                     ('1-1/2-1/4', Fraction(1,4)), ('（1／2＋1／4）', Fraction(3,4)),
                                     ('2*(1/3+1/6)', Fraction(1)), ('0.1+0.2', Fraction(3,10))]:
            if expected is None:
                with self.assertRaises(UnsupportedExpression): parse_expression(expression)
            else:
                self.assertEqual(expected, parse_expression(expression).value)

    def test_parser_rejects_code_excessive_complexity_and_zero_division(self):
        for expression in ['__import__("os")', '2**1000', '1; print(1)', 'x+1', '('*20+'1'+')'*20,
                           '1+'*90+'1', '99999999999999999', '1//2']:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                parse_expression(expression)
        with self.assertRaises(ZeroDivisionError): parse_expression('1/(1-1)')

    def test_step_chain_and_first_error(self):
        correct = check_solution('fraction-add', '1/2 = 2/4\n2/4+1/4 = 3/4')
        self.assertEqual('verified', correct.status)
        result = check_solution('fraction-add', '1/2=2/4\n2/4+1/4=2/6\n3/4=6/8')
        self.assertEqual('incorrect', result.status)
        self.assertEqual(2, result.first_issue)
        self.assertEqual('calculation', result.rows[1]['category'])

    def test_true_unrelated_equalities_and_prose_are_not_verified(self):
        for text in ['1+1=2', '我先通分，因为每一份大小要相同', '1/2+1/4=3/4\n所以我已经掌握了所有分数']:
            with self.subTest(text=text):
                self.assertEqual('needs_review', check_solution('fraction-add', text).status)

    def test_repeating_expression_is_only_partial(self):
        self.assertEqual('partial', check_solution('fraction-add', '1/2+1/4=1/2+1/4').status)
        self.assertEqual('partial', check_solution('fraction-add', '1/2=2/4').status)

    def test_remaining_can_use_parentheses_or_sequential_subtraction(self):
        for text in ['1-(1/2+1/4)=1/4', '1-1/2-1/4=1/4',
                     '已用：1/2+1/4=3/4\n剩余：1-3/4=1/4']:
            with self.subTest(text=text):
                self.assertEqual('verified', check_solution('fraction-word', text).status)

    def test_used_result_does_not_complete_remaining_problem(self):
        self.assertEqual('partial', check_solution('fraction-word', '1/2+1/4=3/4').status)
        report = check_solution('fraction-word', '剩余：1/2+1/4=3/4')
        self.assertEqual('incorrect', report.status)
        self.assertEqual('quantity', report.rows[0]['category'])

    def test_correct_final_answer_does_not_hide_bad_work(self):
        result = self.workspace.run('1', 'fraction-add', '', answer_submission='3/4', solution_steps='1/2+1/4=2/6')
        self.assertEqual('correct', result.reply.answer_check['status'])
        self.assertEqual('incorrect', result.reply.step_check['status'])
        self.assertIn('检查第 1 行', result.reply.explanation)
        self.assertNotEqual('解释为什么分母保持不变', result.reply.next_action)

    def test_step_check_never_writes_state_without_confirmation(self):
        result = self.workspace.run('1', 'fraction-add', '', solution_steps='1/2+1/4=3/4')
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.workspace.decide('fraction-add', result.proposal, 'accept')
        saved = self.workspace.assistant('fraction-add').snapshot()
        self.assertEqual([], saved.task.completed_steps)
        self.assertEqual(1, saved.metadata.version)

    def test_deduplication_includes_steps_and_answer(self):
        result = self.workspace.run('1', 'fraction-add', '', solution_steps='1/2+1/4=3/4')
        self.assertIs(result, self.workspace.run('1', 'fraction-add', '', solution_steps='1/2+1/4=3/4'))
        with self.assertRaises(ValueError):
            self.workspace.run('1', 'fraction-add', '', solution_steps='1/2+1/4=2/6')
        with self.assertRaises(ValueError):
            self.workspace.run('1', 'fraction-add', '', answer_submission='3/4')
        self.assertEqual(1, self.workspace.generation_count)

    def test_explicit_answer_field_overrides_old_inline_marker(self):
        result = self.workspace.run('1', 'fraction-add', '上次写了\n答案：2/6', answer_submission='6/8')
        self.assertEqual('correct', result.reply.answer_check['status'])
        self.assertEqual('6/8', result.reply.answer_check['submitted'])

    def test_no_search_results_does_not_skip_step_or_answer_check(self):
        result = self.workspace.run('1', 'fraction-word', '查笔记：行星轨道',
                                    solution_steps='1-1/2-1/4=1/4', answer_submission='1/4')
        self.assertEqual([], result.sources)
        self.assertEqual('verified', result.reply.step_check['status'])
        self.assertEqual('correct', result.reply.answer_check['status'])

    def test_variants_use_own_numbers_and_isolated_state(self):
        for task_id in ['fraction-add-thirds', 'fraction-word-eighths']:
            left, right = OPERANDS[task_id]
            expected = left+right if task_id.startswith('fraction-add') else 1-left-right
            self.assertEqual(expected, ANSWERS[task_id])
            self.assertEqual('correct', check_answer(task_id, str(expected)).status)
        a = self.workspace.run('a', 'fraction-add-thirds', '', solution_steps='1/3+1/6=1/2')
        b = self.workspace.run('b', 'fraction-word-eighths', '', solution_steps='1-3/8-1/4=3/8')
        self.assertEqual('verified', a.reply.step_check['status'])
        self.assertEqual('verified', b.reply.step_check['status'])
        self.workspace.decide('fraction-add-thirds', a.proposal, 'accept')
        self.assertFalse((self.root/'fraction-add.json').exists())
        restored = Workspace(self.root)
        self.assertEqual(asdict(self.workspace.assistant('fraction-add-thirds').snapshot()),
                         asdict(restored.assistant('fraction-add-thirds').snapshot()))
        self.assertEqual(0, restored.assistant('fraction-word-eighths').snapshot().metadata.version)

    def test_unknown_steps_do_not_generate_progress_update(self):
        result = self.workspace.run('1', 'fraction-add', '', solution_steps='我认为这样是对的')
        self.assertEqual('needs_review', result.reply.step_check['status'])
        self.assertIsNone(result.proposal)
        self.assertFalse((self.root / 'fraction-add.json').exists())
        row = json.loads((self.root / 'runs.jsonl').read_text())
        self.assertEqual('我认为这样是对的', row['solution_steps'])
        self.assertEqual('needs_review', row['reply']['step_check']['status'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
