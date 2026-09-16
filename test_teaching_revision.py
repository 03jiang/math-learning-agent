"""教学边界的回归与本机 Chat HTTP 确认测试；无真实模型调用。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from core import LearningSettings, ValidationError
from curriculum import STEPS, MockTutor, check_answer, initial_snapshot
from http_test_support import LocalModelServer, chat_envelope
from model_api import ApiTutor, HttpTransport, load_model_config
from model_boundary import ReplayTutor, build_model_request, parse_model_response
from teaching_guard import TeachingContractError
from workflow import Workspace


def request(task='fraction-add', **kwargs):
    return build_model_request('请帮助我', initial_snapshot(task), LearningSettings(), [], **kwargs)


def response(req, explanation='先比较每一份的大小。', **changes):
    action = next(iter(req['context']['allowed_next_actions']), None)
    value = {'schema_version': 1, 'explanation': explanation, 'optional_hint': None,
             'next_action': action, 'proposed_state_update': {'current_step': action} if action else None,
             'proposed_configuration_update': None, 'cited_source_ids': []}
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


class TeachingGuardTests(unittest.TestCase):
    def test_observed_answer_based_method_claim_and_optional_hint_are_rejected(self):
        req = request(answer_submission='2/6')
        for field in ('explanation', 'optional_hint'):
            for text in ('你的结果 2/6 说明分母也被相加了。',
                         '你的答案表明你把分子和分母分别相加。',
                         '这个结果证明你把分母加在一起了。'):
                with self.subTest(field=field, text=text), self.assertRaises(TeachingContractError) as caught:
                    parse_model_response(response(req, **{field: text}), req)
                self.assertEqual('unsupported_method_claim', caught.exception.code)
                self.assertEqual({'reason': 'answer_is_not_method_evidence'}, caught.exception.details)

    def test_uncertainty_questions_and_explicit_step_discussion_are_allowed(self):
        req = request(answer_submission='2/6', solution_steps='1/2+1/4=2/6')
        for text in ('你的答案 2/6 说明可能把分子和分母分别相加了。',
                     '你的结果 2/6 不能说明你把分母相加了，请写出计算步骤。',
                     '你的结果 2/6 说明你把分母相加了吗？',
                     '你写出的等式 1/2+1/4=2/6 两边不相等，请核对这一步。',
                     '计算分数相加时，不能直接把分母相加。'):
            with self.subTest(text=text):
                self.assertEqual(text, parse_model_response(response(req, text), req)['explanation'])

    def test_method_inference_boundary_also_applies_to_direct_help(self):
        snap = initial_snapshot('fraction-add')
        req = build_model_request('', snap, replace(snap.settings, explanation_mode='direct'), [],
                                  answer_submission='2/6')
        with self.assertRaises(TeachingContractError):
            parse_model_response(response(req, '你的结果 2/6 说明分母也被相加了。'), req)

    def test_cake_answer_checks_use_eating_language_in_request_context(self):
        for answer in ('5/8', '3/8'):
            req = request('fraction-word-eighths', answer_submission=answer)
            feedback = req['context']['local_checks']['answer']['feedback']
            self.assertIn('吃', feedback)
            self.assertNotIn('用去', feedback)
            self.assertNotIn('已用', feedback)
        self.assertIn('一共用去', check_answer('fraction-word', '3/4').feedback)
        self.assertIn('已用部分', check_answer('fraction-word', '1/4').feedback)

    def test_cake_support_settings_never_reuse_ribbon_terms(self):
        for index, step in enumerate(STEPS['fraction-word-eighths']):
            for mode in ('hint', 'example', 'direct'):
                with self.subTest(index=index, mode=mode):
                    snap = initial_snapshot('fraction-word-eighths')
                    snap.task.current_step = step
                    settings = replace(snap.settings, explanation_mode=mode, structure_level='guided',
                                       pattern_guidance='on', presentation_density='detailed')
                    reply = MockTutor().answer('', snap, settings, [], False, answer_submission='5/8')
                    self.assertIn('吃', reply.explanation)
                    for word in ('用去', '已用', '全长'):
                        self.assertNotIn(word, reply.explanation)

    def test_next_format_examples_roundtrip_to_pending_proposals_for_all_tasks(self):
        for task in STEPS:
            for variant in ('baseline', 'protocol'):
                with self.subTest(task=task, variant=variant), tempfile.TemporaryDirectory() as directory:
                    req = request(task, help_action='next', prompt_variant=variant)
                    raw = req['instructions'].splitlines()[-1]
                    parsed = parse_model_response(raw, req)
                    self.assertIn(parsed['next_action'], STEPS[task])
                    self.assertEqual({'current_step': parsed['next_action']}, parsed['proposed_state_update'])
                    workspace = Workspace(directory, tutor=ReplayTutor(raw))
                    before = workspace.assistant(task).snapshot()
                    result = workspace.run('example', task, '等我确认后再保存', help_action='next')
                    self.assertIsNone(result.error)
                    self.assertIsNotNone(result.proposal)
                    self.assertEqual(before, workspace.assistant(task).snapshot())
                    self.assertFalse((Path(directory) / f'{task}.json').exists())

    def test_format_examples_do_not_require_advance_for_same_step_blocked_or_normal_help(self):
        for task in STEPS:
            snap = initial_snapshot(task)
            snap.task.current_step = STEPS[task][0]
            cases = (build_model_request('', snap, snap.settings, [], help_action='next'),
                     request(task, help_action='next', solution_steps='凭感觉'),
                     request(task, help_action='hint'))
            for req in cases:
                with self.subTest(task=task, help=req['context']['requested_help']):
                    parsed = parse_model_response(req['instructions'].splitlines()[-1], req)
                    self.assertIsNone(parsed['proposed_state_update'])
                    if req['context']['task']['current_step'] == snap.task.current_step:
                        self.assertEqual(snap.task.current_step, parsed['next_action'])
                    else:
                        self.assertIsNone(parsed['next_action'])

    def test_seen_failure_calculations_and_conclusions_are_rejected(self):
        cases = [('fraction-add', '2/4 + 1/4 = 3/4。'),
                 ('fraction-word', '因此还剩全长的 1/4。'),
                 ('fraction-word', '再算剩下：1 - 3/4 = 1/4。'),
                 ('fraction-add-thirds', '2/6 + 1/6 = 3/6，也就是 1/2。')]
        for task, text in cases:
            with self.subTest(task=task, text=text), self.assertRaises(TeachingContractError) as caught:
                req = request(task)
                parse_model_response(response(req, text), req)
            self.assertEqual('hint_answer_revealed', caught.exception.code)

    def test_equivalent_decimal_chinese_and_formatted_answers_are_rejected(self):
        for text in ('答案是 6/8。', '结果为 0.75。', '答案是四分之三。',
                     '最终答案是十二分之九。', '答案是 **３／４**。', r'答案是 $\frac{3}{4}$。'):
            with self.subTest(text=text), self.assertRaises(TeachingContractError):
                req = request()
                parse_model_response(response(req, text), req)

    def test_optional_hint_is_checked_too(self):
        req = request()
        with self.assertRaises(TeachingContractError):
            parse_model_response(response(req, optional_hint='结果是 3/4。'), req)

    def test_question_numbers_and_intermediate_conversions_are_allowed(self):
        for task, text in [('fraction-add', '把 1/2 改写成 2/4，再想能不能合并。'),
                           ('fraction-word', '第二次用去全长的 1/4。1/4 = 2/8。'),
                           ('fraction-word-eighths', '上午吃了整个蛋糕的 3/8，下午吃了 1/4。'),
                           ('fraction-word-eighths', '先求已吃：3/8 + 1/4 = 5/8，再由你算剩余。')]:
            with self.subTest(task=task):
                req = request(task)
                self.assertEqual(text, parse_model_response(response(req, text), req)['explanation'])

    def test_negated_or_incorrect_candidate_not_mistaken_for_correct_answer(self):
        for text in ('答案不是 3/4。', '不要把结果写成 3/4。', '你的答案是 2/6，需要再想一想。'):
            req = request()
            self.assertEqual(text, parse_model_response(response(req, text), req)['explanation'])
        # 本项只测试披露匹配，不能证明这些文字的数学语义正确。

    def test_already_correct_explicit_and_inline_answer_can_be_acknowledged(self):
        for req in (request(answer_submission='6/8', solution_steps='1/2=1/9'),
                    build_model_request('答案：0.75', initial_snapshot('fraction-add'), LearningSettings(), [])):
            text = '你的答案是 3/4，但不能据此判断理由完整。'
            self.assertEqual(text, parse_model_response(response(req, text), req)['explanation'])

    def test_direct_and_example_can_provide_full_calculation(self):
        for mode in ('direct', 'example'):
            snap = initial_snapshot('fraction-add')
            req = build_model_request('帮助', snap, replace(snap.settings, explanation_mode=mode), [])
            self.assertIsNotNone(parse_model_response(response(req, '2/4 + 1/4 = 3/4。'), req))

    def test_next_button_overrides_direct_help_amount(self):
        snap = initial_snapshot('fraction-add')
        req = build_model_request('', snap, replace(snap.settings, explanation_mode='direct'), [], help_action='next')
        with self.assertRaises(TeachingContractError):
            parse_model_response(response(req, '结果是 3/4。'), req)

    def test_explicit_next_requires_proposal_but_ordinary_help_may_omit_it(self):
        req = request()
        self.assertIsNone(parse_model_response(response(req, proposed_state_update=None), req)['proposed_state_update'])
        req = request(help_action='next')
        for changes in ({'proposed_state_update': None}, {'next_action': None, 'proposed_state_update': None}):
            with self.subTest(changes=changes), self.assertRaises(TeachingContractError) as caught:
                parse_model_response(response(req, **changes), req)
            self.assertEqual('missing_next_proposal', caught.exception.code)

    def test_same_step_does_not_force_a_fake_advance(self):
        snap = initial_snapshot('fraction-add'); snap.task.current_step = STEPS['fraction-add'][0]
        req = build_model_request('', snap, snap.settings, [], help_action='next')
        for patch in (None, {'current_step': snap.task.current_step}):
            parsed = parse_model_response(response(req, proposed_state_update=patch), req)
            self.assertIsNone(parsed['proposed_state_update'])

    def test_blocked_progress_remains_without_proposal_even_for_next(self):
        req = request(help_action='next', solution_steps='凭感觉')
        self.assertEqual([], req['context']['allowed_next_actions'])
        parsed = parse_model_response(response(req, '先补一个等式，才能核对过程。'), req)
        self.assertIsNone(parsed['next_action'])
        self.assertIsNone(parsed['proposed_state_update'])

    def test_cake_actions_do_not_reuse_ribbon_language(self):
        req = request('fraction-word-eighths', help_action='next')
        self.assertFalse(any('全长' in action or '用去' in action for action in req['context']['allowed_next_actions']))
        self.assertIn('求上午和下午一共吃了整个蛋糕的几分之几', req['context']['allowed_next_actions'])
        self.assertIn('求两次一共用去全长的几分之几', request('fraction-word')['context']['allowed_next_actions'])


class RevisionChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.server = self.enterContext(LocalModelServer())
        self.server.body = chat_envelope
        config = load_model_config(Path(__file__).parent / 'model_config.deepseek.example.json')
        self.tutor = ApiTutor(config, 'local-test-key', transport=HttpTransport(self.server.chat_url))
        self.workspace = Workspace(self.root, tutor=self.tutor)

    def test_next_accept_edit_reject_duplicate_stale_and_restart(self):
        task = 'fraction-word-eighths'; path = self.root / f'{task}.json'
        assistant = self.workspace.assistant(task); before = assistant.snapshot()
        rejected = self.workspace.run('reject-next', task, '', help_action='next')
        self.assertIsNone(rejected.error)
        self.assertFalse(path.exists())
        self.assertIsNotNone(rejected.proposal)
        wire = json.loads(self.server.requests[-1]['payload']['messages'][1]['content'])['context']
        self.assertEqual('next', wire['requested_help'])
        payload = self.server.requests[-1]['payload']
        example = parse_model_response(payload['messages'][0]['content'].splitlines()[-1], {'context': wire})
        self.assertEqual({'current_step': example['next_action']}, example['proposed_state_update'])
        self.workspace.decide(task, rejected.proposal, 'reject')
        self.assertEqual(before, assistant.snapshot())
        self.assertFalse(path.exists())
        accepted = self.workspace.run('accept-next', task, '', help_action='next')
        stale = self.workspace.run('stale-next', task, '', help_action='next')
        self.workspace.decide(task, accepted.proposal, 'accept')
        saved = path.read_bytes()
        self.assertEqual('already_applied', self.workspace.decide(task, accepted.proposal, 'accept')[0])
        self.assertEqual(saved, path.read_bytes())
        with self.assertRaises(ValidationError):
            self.workspace.decide(task, stale.proposal, 'accept')
        self.assertEqual(saved, path.read_bytes())

        def second_step(payload):
            envelope = chat_envelope(payload)
            value = json.loads(envelope['choices'][0]['message']['content'])
            value.update(next_action=STEPS[task][1], proposed_state_update={'current_step': STEPS[task][1]})
            envelope['choices'][0]['message']['content'] = json.dumps(value, ensure_ascii=False)
            return envelope
        self.server.body = second_step
        editable = self.workspace.run('edit-next', task, '', help_action='next')
        self.assertIsNone(editable.error)
        self.workspace.decide(task, editable.proposal, 'edit', edited_state={'current_step': '先把蛋糕画成八等份'})
        restored = Workspace(self.root).assistant(task).snapshot()
        self.assertEqual('先把蛋糕画成八等份', restored.task.current_step)
        self.assertEqual([], restored.task.completed_steps)
        self.assertEqual(2, restored.metadata.version)
        self.assertEqual(4, len(self.server.requests))
        self.assertEqual(0, sum(result.real_api_calls for result in self.workspace.rounds.values()))
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_leaked_reply_is_not_shown_logged_or_retried(self):
        def leak(payload):
            envelope = chat_envelope(payload)
            value = json.loads(envelope['choices'][0]['message']['content'])
            value['explanation'] = 'PRIVATE_BLOCKED_REPLY 2/4 + 1/4 = 3/4。'
            envelope['choices'][0]['message']['content'] = json.dumps(value)
            return envelope
        self.server.body = leak
        result = self.workspace.run('leak', 'fraction-add', '', answer_submission='2/6', help_action='hint')
        self.assertEqual('hint_answer_revealed', result.model_calls[0]['error_code'])
        self.assertIsNone(result.reply)
        self.assertIsNone(result.proposal)
        self.assertEqual('incorrect', result.local_checks['answer']['status'])
        self.assertIs(result, self.workspace.run('leak', 'fraction-add', '', answer_submission='2/6', help_action='hint'))
        self.assertEqual(1, len(self.server.requests))
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.assertNotIn('PRIVATE_BLOCKED_REPLY', (self.root / 'runs.jsonl').read_text())

    def test_method_claim_returns_specific_error_without_logging_or_retrying(self):
        def claim(payload):
            envelope = chat_envelope(payload)
            value = json.loads(envelope['choices'][0]['message']['content'])
            value['explanation'] = 'PRIVATE_METHOD_CLAIM 你的结果 2/6 说明分母也被相加了。'
            envelope['choices'][0]['message']['content'] = json.dumps(value)
            return envelope
        self.server.body = claim
        result = self.workspace.run('claim', 'fraction-add', '', answer_submission='2/6', help_action='hint')
        self.assertEqual('unsupported_method_claim', result.model_calls[0]['error_code'])
        self.assertEqual({'reason': 'answer_is_not_method_evidence'}, result.model_calls[0]['validation_details'])
        self.assertIn('仅凭答案', result.error)
        self.assertIsNone(result.reply)
        self.assertIsNone(result.proposal)
        self.assertEqual('incorrect', result.local_checks['answer']['status'])
        self.assertIs(result, self.workspace.run('claim', 'fraction-add', '', answer_submission='2/6', help_action='hint'))
        self.assertEqual(1, len(self.server.requests))
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.assertNotIn('PRIVATE_METHOD_CLAIM', (self.root / 'runs.jsonl').read_text())

    def test_null_next_response_reports_specific_error_without_fabricating_update(self):
        for reason in ('missing_next_action', 'missing_state_update'):
            with self.subTest(reason=reason):
                def empty(payload):
                    envelope = chat_envelope(payload)
                    value = json.loads(envelope['choices'][0]['message']['content'])
                    value['proposed_state_update'] = None
                    value['explanation'] = 'PRIVATE_REJECTED_CONTENT 请先看题干。'
                    if reason == 'missing_next_action':
                        value['next_action'] = None
                    envelope['choices'][0]['message']['content'] = json.dumps(value)
                    return envelope
                self.server.body = empty
                before = self.workspace.assistant('fraction-add').snapshot()
                calls = len(self.server.requests)
                result = self.workspace.run(reason, 'fraction-add', '', help_action='next')
                self.assertEqual('missing_next_proposal', result.model_calls[0]['error_code'])
                self.assertEqual({'reason': reason}, result.model_calls[0]['validation_details'])
                self.assertIsNone(result.proposal)
                self.assertIsNone(result.reply)
                self.assertEqual(before, self.workspace.assistant('fraction-add').snapshot())
                self.assertFalse((self.root / 'fraction-add.json').exists())
                self.assertIs(result, self.workspace.run(reason, 'fraction-add', '', help_action='next'))
                self.assertEqual(calls + 1, len(self.server.requests))
                log = (self.root / 'runs.jsonl').read_text()
                self.assertIn(reason, log)
                self.assertNotIn('PRIVATE_REJECTED_CONTENT', log)
                self.assertNotIn('local-test-key', log)


if __name__ == '__main__':
    unittest.main(verbosity=2)
