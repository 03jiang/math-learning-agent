from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

from legacy.curriculum import ANSWERS, QUESTIONS, STEPS, check_answer
from legacy.workflow import Workspace


class MathTeachingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root)

    def test_reference_answers_are_exact(self):
        self.assertEqual(Fraction(1, 2) + Fraction(1, 4), ANSWERS['fraction-add'])
        self.assertEqual(1 - (Fraction(1, 2) + Fraction(1, 4)), ANSWERS['fraction-word'])
        self.assertIn('全长的 1/4', QUESTIONS['fraction-word'])

    def test_equivalent_fraction_and_decimal_answers(self):
        for value in ['3/4', '6/8', '0.75', '0.7500', ' 3 / 4 ', '3／4']:
            with self.subTest(value=value):
                self.assertEqual('correct', check_answer('fraction-add', value).status)
        for value in ['1/4', '2/8', '0.25']:
            self.assertEqual('correct', check_answer('fraction-word', value).status)

    def test_invalid_and_non_numeric_inputs_never_execute(self):
        for value in ['1/0', '0/0', 'NaN', 'inf', '1/2+1/4', '__import__("os").system("echo unsafe")', '', '1' * 90, '-3/-4']:
            with self.subTest(value=value):
                self.assertEqual('invalid', check_answer('fraction-add', value).status)
        self.assertEqual('incorrect', check_answer('fraction-add', '-3/4').status)

    def test_fraction_addition_error_feedback_is_specific(self):
        result = self.workspace.run('1', 'fraction-add', '答案：2/6')
        self.assertEqual('incorrect', result.reply.answer_check['status'])
        self.assertIn('检查是否把分母也相加', result.reply.explanation)
        self.assertEqual(STEPS['fraction-add'][1], result.reply.next_action)

    def test_word_problem_separates_used_and_remaining(self):
        result = self.workspace.run('1', 'fraction-word', '答案：3/4')
        self.assertEqual('incorrect', result.reply.answer_check['status'])
        self.assertIn('一共用去', result.reply.explanation)
        self.assertIn('问还剩多少', result.reply.explanation)

    def test_units_are_not_accepted_for_fraction_of_whole(self):
        result = self.workspace.run('1', 'fraction-word', '答案：1/4米')
        self.assertEqual('invalid_unit', result.reply.answer_check['status'])
        self.assertTrue(result.reply.answer_check['numeric_match'])
        self.assertIsNone(result.proposal)
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_correct_answer_never_marks_mastery_or_completion(self):
        result = self.workspace.run('1', 'fraction-add', '答案：3/4')
        self.assertEqual('correct', result.reply.answer_check['status'])
        self.assertIn('再说说', result.reply.explanation)
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.workspace.decide('fraction-add', result.proposal, 'accept')
        snapshot = self.workspace.assistant('fraction-add').snapshot()
        self.assertEqual([], snapshot.task.completed_steps)
        self.assertNotIn('mastery', asdict(snapshot.task))
        self.assertEqual(STEPS['fraction-add'][-1], snapshot.task.current_step)

    def test_accepting_help_then_explicit_next_step_preserves_completion(self):
        first = self.workspace.run('1', 'fraction-add', '给一个小提示')
        self.workspace.decide('fraction-add', first.proposal, 'accept')
        second = self.workspace.run('2', 'fraction-add', '继续下一步')
        self.assertEqual(STEPS['fraction-add'][1], second.reply.next_action)
        self.assertEqual([], self.workspace.assistant('fraction-add').snapshot().task.completed_steps)

    def test_same_answer_is_evaluated_against_current_task_only(self):
        a = self.workspace.run('1', 'fraction-add', '答案：3/4')
        b = self.workspace.run('2', 'fraction-word', '答案：3/4')
        self.assertEqual('correct', a.reply.answer_check['status'])
        self.assertEqual('incorrect', b.reply.answer_check['status'])

    def test_explicit_direct_help_and_hint_are_different(self):
        hint = self.workspace.run('1', 'fraction-add', '请给小提示')
        direct = self.workspace.run('2', 'fraction-add', '请直接告诉我怎么做')
        self.assertNotIn('= 3/4', hint.reply.explanation)
        self.assertIn('= 3/4', direct.reply.explanation)
        self.assertIsNone(direct.reply.optional_hint)


if __name__ == '__main__':
    unittest.main(verbosity=2)
