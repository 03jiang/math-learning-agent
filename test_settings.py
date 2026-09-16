"""六项设置的行为、旧存档兼容、确认边界与组合回归。"""
from copy import deepcopy
from dataclasses import asdict, replace
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import (LEGACY_SETTING_FIELDS, SETTING_OPTIONS, LearningAssistant,
                  LearningSettings, ValidationError, decode_snapshot)
from curriculum import ANSWERS, TASKS, MockTutor, initial_snapshot
from workflow import Workspace


ENABLED = {'structure_level': 'guided', 'pattern_guidance': 'on', 'scope_support': 'connected'}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'fraction-add.json'
        self.workspace = Workspace(self.root)
        self.assistant = self.workspace.assistant('fraction-add')

    def legacy_file(self):
        snapshot = initial_snapshot('fraction-add')
        snapshot.settings.explanation_mode = 'example'
        snapshot.task.current_step = '先画一个整体'
        snapshot.task.deferred_ideas = ['以后再看数轴']
        snapshot.metadata.version = 7
        snapshot.metadata.applied_proposal_ids = ['old-confirmation']
        data = asdict(snapshot)
        data['settings'] = {key: value for key, value in data['settings'].items() if key in LEGACY_SETTING_FIELDS}
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return data

    def help(self, task_id='fraction-add', **settings):
        snapshot = initial_snapshot(task_id)
        return MockTutor().answer('帮助我理解这道题', snapshot, LearningSettings(**settings), [], False)

    def test_all_six_defaults_are_legal_and_each_field_is_confirmable(self):
        self.assertEqual(6, len(SETTING_OPTIONS))
        self.assertEqual(set(SETTING_OPTIONS), set(asdict(LearningSettings())))
        for name, values in SETTING_OPTIONS.items():
            with self.subTest(name=name):
                self.assertIn(getattr(LearningSettings(), name), values)
                proposal = self.assistant.propose(configuration_patch={name: values[-1]})
                self.assistant.confirm(proposal)
                self.assertEqual(values[-1], getattr(self.assistant.snapshot().settings, name))
        self.assertEqual(6, self.assistant.snapshot().metadata.version)
        self.assertEqual([], self.assistant.snapshot().task.completed_steps)

    def test_legacy_read_supplies_defaults_without_writing_or_mutating_input(self):
        data = self.legacy_file()
        before = self.path.read_bytes()
        original = deepcopy(data)
        restored = decode_snapshot(data)
        self.assertEqual(original, data)
        self.assertEqual('free', restored.settings.structure_level)
        self.assertEqual('off', restored.settings.pattern_guidance)
        self.assertEqual('focused', restored.settings.scope_support)
        self.assertEqual('example', restored.settings.explanation_mode)
        for _ in range(2):
            self.assertEqual(restored, LearningAssistant(self.path).snapshot())
        self.assertEqual(before, self.path.read_bytes())

    def test_unconfirmed_and_rejected_settings_leave_legacy_bytes_unchanged(self):
        self.legacy_file()
        before = self.path.read_bytes()
        proposal = self.assistant.propose(configuration_patch=ENABLED)
        self.assertEqual(before, self.path.read_bytes())
        self.workspace.decide('fraction-add', proposal, 'reject')
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(ValidationError):
            self.assistant.confirm(proposal)
        self.assertEqual(before, self.path.read_bytes())

    def test_confirm_upgrades_legacy_once_and_real_new_process_restores_it(self):
        old = self.legacy_file()
        proposal = self.assistant.propose(configuration_patch=ENABLED)
        self.assertEqual('applied', self.assistant.confirm(proposal))
        saved = self.path.read_bytes()
        data = json.loads(saved)
        self.assertEqual(set(SETTING_OPTIONS), set(data['settings']))
        self.assertEqual(old['task'], data['task'])
        self.assertEqual(8, data['metadata']['version'])
        self.assertEqual(['old-confirmation', proposal.proposal_id], data['metadata']['applied_proposal_ids'])
        self.assertEqual('already_applied', LearningAssistant(self.path).confirm(proposal))
        self.assertEqual(saved, self.path.read_bytes())
        script = ('import json,sys; from dataclasses import asdict; from core import LearningAssistant; '
                  'print(json.dumps(asdict(LearningAssistant(sys.argv[1]).snapshot()),ensure_ascii=False))')
        process = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                 cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
        self.assertEqual(data, json.loads(process.stdout))

    def test_edit_changes_only_confirmed_fields_and_keeps_other_tasks_separate(self):
        proposal = self.assistant.propose(configuration_patch=ENABLED)
        edited = {**ENABLED, 'scope_support': 'focused'}
        self.workspace.decide('fraction-add', proposal, 'edit', edited_configuration=edited)
        restored = Workspace(self.root)
        current = restored.assistant('fraction-add').snapshot()
        for name, value in edited.items():
            self.assertEqual(value, getattr(current.settings, name))
        self.assertEqual(initial_snapshot('fraction-add').task, current.task)
        for task_id in TASKS:
            if task_id != 'fraction-add':
                self.assertEqual(LearningSettings(), restored.assistant(task_id).snapshot().settings)
                self.assertFalse((self.root / f'{task_id}.json').exists())

    def test_partial_unknown_or_invalid_legacy_settings_are_not_silently_repaired(self):
        original = self.legacy_file()
        invalid_settings = [
            {**original['settings'], 'structure_level': 'guided'},
            {**original['settings'], 'structure_level': 'guided', 'pattern_guidance': 'on'},
            {'step_size': 'small'},
            {**asdict(LearningSettings()), 'unexpected': 'on'},
            {**original['settings'], 'step_size': 'huge'},
            {**asdict(LearningSettings()), 'scope_support': None},
        ]
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                self.path.write_text(json.dumps({**original, 'settings': settings}))
                before = self.path.read_bytes()
                with self.assertRaises(ValidationError):
                    LearningAssistant(self.path)
                self.assertEqual(before, self.path.read_bytes())

    def test_invalid_expired_or_stale_new_settings_cannot_write(self):
        self.legacy_file()
        before = self.path.read_bytes()
        for config in [{'structure_level': True}, {'pattern_guidance': 'auto'}, {'scope_support': ['connected']}]:
            with self.subTest(config=config):
                proposal = self.assistant.propose(configuration_patch=config)
                with self.assertRaises(ValidationError):
                    self.assistant.confirm(proposal)
                self.assertEqual(before, self.path.read_bytes())
        expired = self.assistant.propose(configuration_patch=ENABLED)
        with patch('core.time.time', return_value=expired.expires_at):
            with self.assertRaises(ValidationError):
                self.assistant.confirm(expired)
        self.assertEqual(before, self.path.read_bytes())
        stale = self.assistant.propose(configuration_patch=ENABLED)
        valid = self.assistant.propose(configuration_patch={'pattern_guidance': 'on'})
        self.assistant.confirm(valid)
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            self.assistant.confirm(stale)
        self.assertEqual(before, self.path.read_bytes())

    def test_failed_upgrade_keeps_legacy_file_and_proposal_retryable(self):
        self.legacy_file()
        before = self.path.read_bytes()
        proposal = self.assistant.propose(configuration_patch=ENABLED)
        with patch('core.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                self.assistant.confirm(proposal)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([], list(self.root.glob('.state-*')))
        self.assertEqual('applied', self.assistant.confirm(proposal))

    def test_all_temporary_settings_reset_even_when_next_action_is_accepted(self):
        temporary = {name: values[-1] for name, values in SETTING_OPTIONS.items()}
        first = self.workspace.run('temporary', 'fraction-add', '帮助我理解', temporary)
        self.assertEqual(temporary, first.effective_settings)
        self.assertFalse(self.path.exists())
        self.workspace.decide('fraction-add', first.proposal, 'accept')
        snapshot = self.assistant.snapshot()
        self.assertEqual(LearningSettings(), snapshot.settings)
        self.assertEqual([], snapshot.task.completed_steps)
        second = self.workspace.run('next', 'fraction-add', '继续')
        self.assertEqual(asdict(LearningSettings()), second.effective_settings)
        rows = [json.loads(line) for line in (self.root / 'runs.jsonl').read_text().splitlines()]
        self.assertEqual(temporary, rows[0]['effective_settings'])
        self.assertEqual(asdict(LearningSettings()), rows[0]['input_state']['settings'])
        self.assertEqual('primary-math-api-v2', rows[0]['code_version'])

    def test_reusing_round_id_with_changed_new_setting_is_rejected(self):
        first = self.workspace.run('same', 'fraction-add', '帮助我理解', ENABLED)
        before = (self.root / 'runs.jsonl').read_bytes()
        self.assertIs(first, self.workspace.run('same', 'fraction-add', '帮助我理解', ENABLED))
        with self.assertRaises(ValidationError):
            self.workspace.run('same', 'fraction-add', '帮助我理解', {**ENABLED, 'scope_support': 'focused'})
        self.assertEqual(before, (self.root / 'runs.jsonl').read_bytes())

    def test_structure_supplies_task_specific_sentence_frames(self):
        for task_id in TASKS:
            with self.subTest(task_id=task_id):
                plain = self.help(task_id)
                guided = self.help(task_id, structure_level='guided')
                self.assertNotEqual(plain.explanation, guided.explanation)
                self.assertIn('____', guided.explanation)
                self.assertIn('每一份____' if task_id.startswith('fraction-add') else '要求的是____', guided.explanation)
                self.assertEqual(replace(guided, explanation=plain.explanation), plain)

    def test_pattern_and_scope_supply_different_concepts_independently(self):
        for task_id in TASKS:
            with self.subTest(task_id=task_id):
                plain = self.help(task_id)
                pattern = self.help(task_id, pattern_guidance='on')
                connected = self.help(task_id, scope_support='connected')
                expected = ('单位保持不变' if task_id.startswith('fraction-add')
                            else '整体 − 已吃部分 = 剩余部分' if task_id == 'fraction-word-eighths'
                            else '整体 − 已用部分 = 剩余部分')
                self.assertIn(expected, pattern.explanation)
                self.assertIn('数轴' if task_id.startswith('fraction-add') else '实际数量', connected.explanation)
                self.assertNotIn('____', pattern.explanation)
                self.assertNotIn('**方法提示**', connected.explanation)
                self.assertNotIn('**知识联系', pattern.explanation)
                self.assertEqual(replace(pattern, explanation=plain.explanation), plain)
                self.assertEqual(replace(connected, explanation=plain.explanation), plain)

    def test_hint_support_does_not_add_final_answers_or_switch_tasks(self):
        for task_id in TASKS:
            with self.subTest(task_id=task_id):
                reply = self.help(task_id, **ENABLED)
                self.assertNotIn(str(ANSWERS[task_id]), reply.explanation)
                self.assertEqual(self.help(task_id).proposed_state_update, reply.proposed_state_update)
                self.assertIsNone(reply.proposed_configuration_update)

    def test_all_576_task_setting_combinations_preserve_checking_and_state_boundaries(self):
        tutor = MockTutor()
        combinations = itertools.product(*SETTING_OPTIONS.values())
        for values in combinations:
            settings = LearningSettings(**dict(zip(SETTING_OPTIONS, values)))
            for task_id in TASKS:
                with self.subTest(task_id=task_id, settings=settings):
                    snapshot = initial_snapshot(task_id)
                    before = deepcopy(snapshot)
                    reply = tutor.answer('', snapshot, settings, [], False,
                                         answer_submission=str(ANSWERS[task_id]), solution_steps='1/2 = 2/6')
                    self.assertEqual('correct', reply.answer_check['status'])
                    self.assertEqual('incorrect', reply.step_check['status'])
                    self.assertIn('先修正', reply.explanation)
                    self.assertEqual(before, snapshot)
                    self.assertEqual({'current_step'}, set(reply.proposed_state_update))
                    self.assertIsNone(reply.proposed_configuration_update)

    def test_direct_request_overrides_hint_with_all_support_enabled_for_one_round(self):
        proposal = self.assistant.propose(configuration_patch=ENABLED)
        self.assistant.confirm(proposal)
        before = self.path.read_bytes()
        direct = self.workspace.run('direct', 'fraction-add', '请直接告诉我怎么算', {'explanation_mode': 'hint'})
        self.assertEqual('direct', direct.effective_settings['explanation_mode'])
        self.assertIn('2/4 + 1/4 = 3/4', direct.reply.explanation)
        self.assertIsNone(direct.reply.optional_hint)
        next_round = self.workspace.run('later', 'fraction-add', '帮我理解')
        self.assertEqual('hint', next_round.effective_settings['explanation_mode'])
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_input_and_empty_search_still_create_no_progress_proposal(self):
        for request, message, kwargs in [('invalid-answer', '', {'answer_submission': '1/0'}),
                                          ('text-steps', '', {'solution_steps': '我是凭感觉做的'}),
                                          ('empty-notes', '查笔记：行星轨道', {})]:
            with self.subTest(request=request):
                result = self.workspace.run(request, 'fraction-add', message, ENABLED, **kwargs)
                self.assertIsNone(result.error)
                self.assertIsNone(result.proposal)
                self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
