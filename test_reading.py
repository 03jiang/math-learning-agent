"""两道应用题的选项检查与实际 Streamlit 交互测试；均不连接模型。"""
from dataclasses import asdict
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core import ValidationError
from curriculum import QUESTIONS
from reading_check import READING_TASKS, check_reading, reading_questions

APP = Path(__file__).parent / 'app.py'
MATCHING = {'whole': 'original', 'second_reference': 'original', 'target': 'remaining_fraction'}


class ReadingRuleTests(unittest.TestCase):
    def test_only_the_two_remaining_problems_have_cards(self):
        for task_id in ('fraction-add', 'fraction-add-thirds', 'unknown', None):
            with self.subTest(task_id=task_id), self.assertRaises(ValidationError):
                reading_questions(task_id)
        for task_id in READING_TASKS:
            questions = reading_questions(task_id)
            self.assertEqual(3, len(questions))
            quoted = '第二次用去全长的 1/4' if task_id == 'fraction-word' else '下午吃了整个蛋糕的 1/4'
            self.assertIn(quoted, QUESTIONS[task_id])
            self.assertIn(quoted, questions[1].retry_feedback)

    def test_unanswered_is_incomplete_and_not_a_wrong_answer(self):
        report = check_reading('fraction-word', {})
        self.assertEqual('incomplete', report.status)
        self.assertEqual({'unanswered'}, {row['status'] for row in report.rows})
        partial = check_reading('fraction-word', {'whole': 'original', 'second_reference': None})
        self.assertEqual('correct', partial.rows[0]['status'])
        self.assertEqual('unanswered', partial.rows[1]['status'])
        self.assertEqual('incomplete', partial.status)

    def test_original_whole_and_remaining_whole_are_distinguished(self):
        for task_id in READING_TASKS:
            with self.subTest(task_id=task_id):
                report = check_reading(task_id, {**MATCHING, 'second_reference': 'remaining'})
                self.assertEqual('retry', report.status)
                self.assertEqual(['correct', 'incorrect', 'correct'], [row['status'] for row in report.rows])
                self.assertIn('另一道题', report.rows[1]['feedback'])
                self.assertIn('原来', report.rows[1]['feedback'])

    def test_used_fraction_and_actual_quantity_are_not_the_target(self):
        for task_id in READING_TASKS:
            for target in ('used_fraction', 'remaining_quantity'):
                with self.subTest(task_id=task_id, target=target):
                    report = check_reading(task_id, {**MATCHING, 'target': target})
                    self.assertEqual('retry', report.status)
                    self.assertEqual('incorrect', report.rows[2]['status'])
                    self.assertIn('中间量', report.rows[2]['feedback'])
                    self.assertIn('题目也没有给实际数量', report.rows[2]['feedback'])

    def test_only_matching_choices_pass_and_result_is_not_mastery(self):
        original = deepcopy(MATCHING)
        for task_id in READING_TASKS:
            report = check_reading(task_id, MATCHING)
            self.assertEqual('matched', report.status)
            self.assertEqual({'correct'}, {row['status'] for row in report.rows})
            self.assertIn('选择与题干相符', report.next_prompt)
            self.assertIn('不判断概念掌握', report.verified_scope)
            self.assertNotIn('completed_steps', asdict(report))
            self.assertNotIn('proposed_state_update', asdict(report))
        self.assertEqual(original, MATCHING)

    def test_unknown_fields_types_or_option_ids_are_rejected(self):
        for value in ([], None, {'score': 100}, {'whole': []}, {'whole': True},
                      {'whole': 'remaining_fraction'}, {'target': '<script>'}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                check_reading('fraction-word', value)

    def test_task_wording_is_specific_and_does_not_supply_final_calculation(self):
        for task_id in READING_TASKS:
            choices = {question.key: question.choices[0][0] for question in reading_questions(task_id)}
            raw = json.dumps(asdict(check_reading(task_id, choices)), ensure_ascii=False)
            self.assertNotIn('= 1/4', raw)
            self.assertNotIn('= 3/8', raw)
            self.assertNotIn('剩下 1/4', raw)
            self.assertNotIn('蛋糕' if task_id == 'fraction-word' else '彩带', raw)


class ReadingAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'model_config.json'
        self.config.write_text((APP.parent / 'model_config.example.json').read_text())
        environment = patch.dict(os.environ, {'MATH_ASSISTANT_DATA_DIR': str(self.root),
                                              'MATH_ASSISTANT_START_VIEW': '四题练习',
                                              'MATH_MODEL_CONFIG': str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.app.selectbox(key='selected_task').select('fraction-word').run()
        self.assertFalse(self.app.exception)

    def choose(self, choices, task_id='fraction-word'):
        for key, value in choices.items():
            self.app.selectbox(key=f'reading-{task_id}-{key}').select(value)

    def submit(self):
        next(item for item in self.app.button if item.label == '检查我的读题').click().run()
        self.assertFalse(self.app.exception)

    def test_no_preselected_answer_and_incomplete_is_not_judged_wrong(self):
        for key in MATCHING:
            self.assertIsNone(self.app.selectbox(key=f'reading-fraction-word-{key}').value)
        self.assertFalse((self.root / 'runs.jsonl').exists())
        self.submit()
        entry = self.app.session_state['reading_practice']['fraction-word']
        self.assertEqual('incomplete', entry['report'].status)
        self.assertTrue(any('还没有选择' in item.value for item in self.app.info))
        self.assertFalse(any(item.label == '接受更新' for item in self.app.button))

    def test_edit_choice_gives_targeted_feedback_without_state_or_model_calls(self):
        workspace = self.app.session_state['workspace']
        before = {key: item.snapshot() for key, item in workspace.assistants.items()}
        self.choose({**MATCHING, 'second_reference': 'remaining', 'target': 'used_fraction'})
        with patch.object(workspace.tutor, 'answer', side_effect=AssertionError('must not call tutor')), \
                patch.object(workspace.tutor, 'plan', side_effect=AssertionError('must not search')):
            self.submit()
            self.assertTrue(any('另一道题' in item.value for item in self.app.warning))
            self.choose(MATCHING)
            self.submit()
        self.assertEqual('matched', self.app.session_state['reading_practice']['fraction-word']['report'].status)
        self.assertEqual(0, workspace.generation_count)
        self.assertEqual({}, workspace.rounds)
        for key, item in workspace.assistants.items():
            self.assertEqual(before[key], item.snapshot())
            self.assertFalse((self.root / f'{key}.json').exists())
        self.assertFalse(any(item.label == '接受更新' for item in self.app.button))
        rows = [json.loads(line) for line in (self.root / 'runs.jsonl').read_text().splitlines()]
        self.assertEqual(['retry', 'matched'], [row['reading_check']['status'] for row in rows])
        self.assertTrue(all(row['real_api_calls'] == 0 and row['event'] == 'reading_check' for row in rows))

    def test_rerun_does_not_duplicate_log_or_check(self):
        self.choose(MATCHING)
        self.submit()
        before = (self.root / 'runs.jsonl').read_bytes()
        self.app.run()
        self.app.run()
        self.assertEqual(before, (self.root / 'runs.jsonl').read_bytes())
        self.assertEqual('matched', self.app.session_state['reading_practice']['fraction-word']['report'].status)

    def test_switching_tasks_restores_only_last_submitted_choices(self):
        self.choose({**MATCHING, 'second_reference': 'remaining'})
        self.submit()
        self.app.selectbox(key='selected_task').select('fraction-word-eighths').run()
        for key in MATCHING:
            self.assertIsNone(self.app.selectbox(key=f'reading-fraction-word-eighths-{key}').value)
        self.choose(MATCHING, 'fraction-word-eighths')
        self.submit()
        self.app.selectbox(key='selected_task').select('fraction-word').run()
        self.assertEqual('remaining', self.app.selectbox(key='reading-fraction-word-second_reference').value)
        self.assertEqual('retry', self.app.session_state['reading_practice']['fraction-word']['report'].status)
        self.app.selectbox(key='selected_task').select('fraction-add').run()
        self.assertFalse(any(item.label == '检查我的读题' for item in self.app.button))
        self.assertFalse(self.app.exception)

    def test_existing_confirmed_state_and_pending_proposal_are_not_changed(self):
        workspace = self.app.session_state['workspace']
        assistant = workspace.assistant('fraction-word')
        initial = assistant.propose({'current_step': '先画彩带'})
        assistant.confirm(initial)
        path = self.root / 'fraction-word.json'
        before = path.read_bytes()
        pending = assistant.propose({'current_step': '找出整体、已知量和所求量'})
        self.choose(MATCHING)
        self.submit()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual('applied', assistant.confirm(pending))
        self.assertEqual([], assistant.snapshot().task.completed_steps)

    def test_new_session_drops_practice_but_restores_confirmed_progress(self):
        workspace = self.app.session_state['workspace']
        assistant = workspace.assistant('fraction-word')
        assistant.confirm(assistant.propose({'current_step': '先画彩带'}))
        before = (self.root / 'fraction-word.json').read_bytes()
        self.choose(MATCHING)
        self.submit()
        restored = AppTest.from_file(str(APP), default_timeout=10).run()
        restored.selectbox(key='selected_task').select('fraction-word').run()
        self.assertIsNone(restored.session_state['reading_practice']['fraction-word']['report'])
        self.assertIsNone(restored.selectbox(key='reading-fraction-word-whole').value)
        self.assertEqual('先画彩带', restored.session_state['workspace'].assistant('fraction-word').snapshot().task.current_step)
        self.assertEqual(before, (self.root / 'fraction-word.json').read_bytes())

    def test_log_failure_preserves_feedback_without_retry_or_state_change(self):
        workspace = self.app.session_state['workspace']
        self.choose(MATCHING)
        with patch.object(workspace, 'log', side_effect=OSError('disk error')) as log:
            self.submit()
            self.app.run()
        self.assertEqual(1, log.call_count)
        self.assertTrue(any('读题记录未能保存' in item.value for item in self.app.warning))
        self.assertEqual('matched', self.app.session_state['reading_practice']['fraction-word']['report'].status)
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_next_help_uses_last_checked_reading_not_unsubmitted_draft(self):
        from model_boundary import ReplayTutor
        from workflow import Workspace
        raw = json.dumps({'schema_version': 1, 'explanation': '先核对第二问中的整体。',
                          'next_action': None, 'optional_hint': None, 'proposed_state_update': None,
                          'proposed_configuration_update': None, 'cited_source_ids': []})
        tutor = ReplayTutor(raw)
        self.app.session_state['workspace'] = Workspace(self.root, tutor=tutor)
        self.choose({**MATCHING, 'second_reference': 'remaining'})
        self.submit()
        self.choose(MATCHING)  # 尚未点击“检查我的读题”的草稿
        next(item for item in self.app.button if item.label == '给个小提示').click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(['second_reference'], tutor.requests[-1]['context']['reading_check']['incorrect_question_ids'])
        self.app.selectbox(key='selected_task').select('fraction-word-eighths').run()
        next(item for item in self.app.button if item.label == '给个小提示').click().run()
        self.assertIsNone(tutor.requests[-1]['context']['reading_check'])
        self.assertFalse((self.root / 'fraction-word.json').exists())


if __name__ == '__main__':
    unittest.main()
