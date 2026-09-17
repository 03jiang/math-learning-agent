"""人工编写的回归用例；检查有限证据边界，不代表模型质量或学生掌握。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from study.corrections import validate_result
from study.diagnosis import validate_analysis, AnalysisValidationError
from study.evidence import ANSWER_ONLY_FEEDBACK, arithmetic_issue
from study.notebook import Notebook, make_entry
from study.output_contract import output_schema

ROOT = Path(__file__).resolve().parent
WORK = '1/2 + 1/4 = (1 + 1)/(2 + 4) = 2/6'


def example(row='b01'):
    return json.loads((ROOT/'evaluation/photo_analysis_local_responses.json').read_text())['responses'][row]


class EvidenceTests(unittest.TestCase):
    def assert_issue(self, value, code, work=WORK, kind='steps'):
        before = deepcopy(value)
        with self.assertRaises(AnalysisValidationError) as caught:
            validate_analysis(value, student_work=work, work_kind=kind)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, value, '不能悄悄修订模型原回复')

    def test_partial_equality_does_not_become_an_independent_wrong_step(self):
        value = example()
        value['student_review']['comparisons'][1].update(student_excerpt='= 2/6', verdict='incorrect')
        self.assert_issue(value, 'step_quote_incomplete')

    def test_valid_local_arithmetic_cannot_be_labelled_wrong_due_to_previous_error(self):
        value = example()
        value['student_review']['comparisons'][1]['verdict'] = 'incorrect'
        self.assert_issue(value, 'step_arithmetic_verdict_mismatch')

    def test_false_numeric_equality_cannot_be_labelled_correct(self):
        value = example()
        value['student_review'].update(verdict='correct')
        value['student_review']['comparisons'] = value['student_review']['comparisons'][:1]
        value['student_review']['comparisons'][0]['verdict'] = 'correct'
        value['diagnosis'] = []
        self.assert_issue(value, 'step_arithmetic_verdict_mismatch')

    def test_mixed_chain_must_be_split_not_assigned_one_wrong_verdict(self):
        value = example()
        value['student_review']['comparisons'] = value['student_review']['comparisons'][:1]
        value['student_review']['comparisons'][0]['student_excerpt'] = WORK
        self.assert_issue(value, 'step_mixed_equalities')

    def test_first_error_and_later_correct_arithmetic_coexist(self):
        value = example()
        self.assertEqual(value, validate_analysis(value, student_work=WORK, work_kind='steps'))
        self.assertEqual(['incorrect', 'correct'], [r['verdict'] for r in value['student_review']['comparisons']])

    def test_numeric_scope_supports_whitespace_unicode_sign_and_decimals(self):
        for quote in ('（1 ＋ 1）／（2 ＋ 4）＝2/6', '-0.5 + 1 = 1/2', '3/9 = 1/3',
                      '2/5 = 8/20，1/4 = 5/20', '2 = 4/2 = 6/3'):
            with self.subTest(quote=quote):
                self.assertIsNone(arithmetic_issue(quote, 'correct'))
                self.assertEqual('step_arithmetic_verdict_mismatch', arithmetic_issue(quote, 'incorrect'))

    def test_unsupported_algebra_prose_and_oversize_are_not_declared_wrong(self):
        for quote in ('x = (11 - 3)/2 = 4', '面积 = 长 × 宽', '1/0 = 2',
                      '9'*200 + ' = 1', "__import__('os').system('echo not-executed') = 1"):
            with self.subTest(quote=quote):
                self.assertIsNone(arithmetic_issue(quote, 'correct'))
                self.assertIsNone(arithmetic_issue(quote, 'incorrect'))

    def test_uncertain_is_not_forced_to_a_definite_verdict(self):
        self.assertIsNone(arithmetic_issue('6 + 2 = 10 = 20/2', 'uncertain'))
        self.assertIsNone(arithmetic_issue('1 + 1 = 3', 'uncertain'))
        self.assertEqual('step_quote_incomplete', arithmetic_issue('= 3', 'uncertain'))

    def test_answer_feedback_cannot_assert_how_an_answer_was_produced(self):
        value = example('b02')
        value['student_review']['answer_feedback'] = '2/6 是把分子、分母分别相加得到的。'
        self.assert_issue(value, 'answer_only_feedback_not_bounded', '2/6', 'answer_only')

    def test_answer_feedback_cannot_append_claim_to_allowed_sentence(self):
        value = example('b02')
        value['student_review']['answer_feedback'] += '你没有通分。'
        self.assert_issue(value, 'answer_only_feedback_not_bounded', '2/6', 'answer_only')

    def test_feedback_matches_verdict_including_clarification(self):
        for verdict, feedback in ANSWER_ONLY_FEEDBACK.items():
            value = example('b02')
            value['student_review'].update(verdict=verdict, answer_feedback=feedback)
            validate_analysis(value, student_work='2/6', work_kind='answer_only')
        value.update(status='needs_clarification', answer='', steps=[], knowledge_points=[],
                     takeaway='', clarification='请补充题目的缺失条件。')
        validate_analysis(value, student_work='2/6', work_kind='answer_only')
        value['student_review']['answer_feedback'] = ANSWER_ONLY_FEEDBACK['correct']
        self.assert_issue(value, 'answer_only_feedback_not_bounded', '2/6', 'answer_only')

    def test_attribution_cannot_be_moved_to_other_explanatory_fields(self):
        for field in ('topic', 'summary', 'answer', 'takeaway', 'next_practice', 'steps', 'knowledge_points'):
            value = example('b02')
            value[field] = ['学生把分子和分母直接相加了。'] if isinstance(value[field], list) else '学生把分子和分母直接相加了。'
            self.assert_issue(value, 'answer_only_method_claim', '2/6', 'answer_only')
        for sentence in ('这个答案是把分母相加得到的。', '你可能把分母相加了。',
                         '错因在于没有通分。', 'The student added the denominators.'):
            value = example('b02'); value['summary'] = sentence
            self.assert_issue(value, 'answer_only_method_claim', '2/6', 'answer_only')

    def test_reference_explanation_and_request_for_work_are_allowed(self):
        value = example('b02')
        value['summary'] = '异分母分数相加应先通分，不能直接相加分母。'
        value['steps'].append('2/4 是把 1/2 的分子和分母同时乘 2 得到的。')
        value['next_practice'] = '请补充你得到这个答案的计算过程；你可以画图说明。'
        validate_analysis(value, student_work='2/6', work_kind='answer_only')

    def test_strict_schema_restricts_feedback_for_analysis_and_correction(self):
        context = {'student_work_kind': 'answer_only', 'student_work': '2/6'}
        for operation in ('analyze', 'reanalyze'):
            schema = output_schema(operation, context=context)
            if operation == 'reanalyze': schema = schema['properties']['analysis']
            review = schema['properties']['student_review']['properties']
            self.assertEqual(list(ANSWER_ONLY_FEEDBACK.values()), review['answer_feedback']['enum'])
            self.assertEqual(['answer_only'], review['work_kind']['enum'])
        # 有步骤仍保留可引用证据的个性化反馈。
        review = output_schema('analyze', context={'student_work_kind':'steps'})['properties']['student_review']['properties']
        self.assertNotIn('enum', review['answer_feedback'])

    def test_old_analysis_reads_unchanged_but_cannot_be_saved_as_new(self):
        value = example('b02'); value['student_review']['answer_feedback'] = '旧版的自由文本反馈。'
        entry = make_entry(uuid4().hex, question='计算 1/2 + 1/4', level='小学', my_work='2/6', analysis=value)
        with tempfile.TemporaryDirectory() as temp:
            old = Notebook(Path(temp)/'old'); old.directory.mkdir()
            old.path(entry['id']).write_text(json.dumps(entry, ensure_ascii=False))
            before = old.path(entry['id']).read_bytes()
            self.assertEqual(entry, old.get(entry['id']))
            self.assertEqual('already_saved', old.save_new(entry))
            self.assertEqual(before, old.path(entry['id']).read_bytes())
            new = Notebook(Path(temp)/'new')
            with self.assertRaises(AnalysisValidationError): new.save_new(entry)
            self.assertFalse(new.directory.exists())

    def test_accepted_valid_analysis_recovers_in_new_process_without_mastery(self):
        with tempfile.TemporaryDirectory() as temp:
            book = Notebook(Path(temp)/'book')
            entry = make_entry(uuid4().hex, question='计算 1/2 + 1/4', level='小学', my_work=WORK, analysis=example())
            self.assertFalse(book.directory.exists())
            book.save_new(entry); before = book.path(entry['id']).read_bytes()
            self.assertEqual('already_saved', book.save_new(entry))
            output = subprocess.check_output([sys.executable, '-B', '-c',
                'import sys,json;from study.notebook import Notebook;print(json.dumps(Notebook(sys.argv[1]).get(sys.argv[2])))',
                str(book.directory), entry['id']], cwd=ROOT, text=True)
            self.assertEqual(entry, json.loads(output))
            self.assertEqual([], json.loads(output)['reviews'])
            self.assertEqual(before, book.path(entry['id']).read_bytes())

    def test_new_correction_rejects_unsupported_feedback_before_changing_notebook(self):
        old = example(); answer = example('b02')
        answer['student_review']['answer_feedback'] = '你把分母相加了。'
        result = {'schema_version':1, 'analysis':answer, 'comparison':{'summary':'请补步骤。','changes':[]}}
        with tempfile.TemporaryDirectory() as temp:
            book = Notebook(temp)
            entry = make_entry(uuid4().hex, question='计算 1/2 + 1/4', level='小学', my_work=WORK, analysis=old)
            book.save_new(entry); before = book.path(entry['id']).read_bytes()
            with self.assertRaises(AnalysisValidationError):
                book.add_correction(entry['id'], 1, uuid4().hex, based_on='original', answer='2/6',
                                    work_kind='answer_only', result=result, analysis_origin='手写测试数据')
            self.assertEqual(before, book.path(entry['id']).read_bytes())

    def test_legacy_correction_read_does_not_apply_new_generation_contract(self):
        value = example('b02'); value['student_review']['answer_feedback'] = '旧版自由反馈。'
        result = {'schema_version':1, 'analysis':value, 'comparison':{'summary':'请补步骤。','changes':[]}}
        args = dict(previous_work=WORK, previous_analysis=example(), answer='2/6', work_kind='answer_only')
        self.assertEqual(result, validate_result(result, **args, allow_legacy=True))
        with self.assertRaises(AnalysisValidationError): validate_result(result, **args)
        entry = make_entry(uuid4().hex, question='计算 1/2 + 1/4', level='小学', my_work=WORK, analysis=example())
        operation = uuid4().hex
        entry['corrections'] = [{'id':operation, 'at':entry['created_at'], 'based_on':'original',
                                'answer':'2/6', 'work_kind':'answer_only', 'result':result, 'analysis_origin':'旧测试数据'}]
        entry['operation_ids'] = [operation]; entry['version'] = 2
        entry['schema_version'] = max(entry['schema_version'], 2)
        with tempfile.TemporaryDirectory() as temp:
            old = Notebook(Path(temp)/'old'); old.directory.mkdir()
            old.path(entry['id']).write_text(json.dumps(entry, ensure_ascii=False))
            before = old.path(entry['id']).read_bytes()
            self.assertEqual(entry, old.get(entry['id']))
            new = Notebook(Path(temp)/'new')
            with self.assertRaises(AnalysisValidationError): new.save_new(entry)
            self.assertFalse(new.directory.exists())
            self.assertEqual(before, old.path(entry['id']).read_bytes())


if __name__ == '__main__': unittest.main()
