"""实际执行 Streamlit 脚本，模拟点击和重跑；不是浏览器视觉测试。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from legacy.core import SETTING_OPTIONS
from legacy.model_boundary import ReplayTutor
from legacy.workflow import Workspace
from legacy.http_test_support import LocalModelServer, chat_envelope
from legacy.model_api import ApiTutor, HttpTransport, ModelConfig, load_model_config

APP = Path(__file__).resolve().parents[1] / 'legacy/app.py'


def button(app, label):
    return next(b for b in app.button if b.label == label)


class StreamlitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config_path = self.root / 'model_config.json'
        config_path.write_text((APP.parent.parent / 'model_config.example.json').read_text())
        self.env = patch.dict(os.environ, {'MATH_ASSISTANT_DATA_DIR': str(self.root), 'MATH_MODEL_CONFIG': str(config_path),
                                           'MATH_ASSISTANT_START_VIEW': '四题练习'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(0, len(self.app.exception))

    def submit(self, message='小提示'):
        task_id = self.app.session_state['selected_task']
        self.app.text_area(key=f'message-{task_id}').set_value(message)
        button(self.app, '提交求助').click().run()
        self.assertEqual(0, len(self.app.exception))

    def test_rerun_never_regenerates_or_reconfirms(self):
        self.submit('查笔记：通分')
        workspace = self.app.session_state['workspace']
        before = (self.root / 'runs.jsonl').read_bytes()
        self.app.run()
        self.assertEqual(1, workspace.generation_count)
        self.assertEqual(before, (self.root / 'runs.jsonl').read_bytes())
        button(self.app, '接受更新').click().run()
        path = self.root / 'fraction-add.json'
        saved = path.read_bytes()
        self.app.run()
        self.assertEqual(saved, path.read_bytes())
        self.assertEqual(1, workspace.generation_count)
        self.assertEqual([], json.loads(saved)['task']['completed_steps'])
        self.assertFalse(any(b.label == '接受更新' for b in self.app.button))

    def test_task_switch_preserves_own_pending(self):
        self.submit()
        self.app.selectbox(key='selected_task').select('fraction-word').run()
        self.assertFalse(any(b.label == '接受更新' for b in self.app.button))
        self.submit()
        button(self.app, '接受更新').click().run()
        self.app.selectbox(key='selected_task').select('fraction-add').run()
        button(self.app, '拒绝更新').click().run()
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.assertEqual('找出整体、已知量和所求量', json.loads((self.root / 'fraction-word.json').read_text())['task']['current_step'])
        self.assertEqual(0, len(self.app.exception))

    def test_temporary_settings_reset_on_next_submit(self):
        self.app.selectbox(key='temp-fraction-add-presentation_density').select('detailed')
        self.submit()
        workspace = self.app.session_state['workspace']
        first = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertEqual('detailed', first.effective_settings['presentation_density'])
        self.submit('继续')
        second = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertEqual('brief', second.effective_settings['presentation_density'])

    def test_edit_and_invalid_empty_step(self):
        self.submit()
        next(x for x in self.app.text_input if x.label.startswith('编辑')).set_value('')
        button(self.app, '保存编辑后的更新').click().run()
        self.assertTrue(self.app.error)
        self.assertFalse((self.root / 'fraction-add.json').exists())
        next(x for x in self.app.text_input if x.label.startswith('编辑')).set_value('先画出四等份')
        button(self.app, '保存编辑后的更新').click().run()
        self.assertEqual('先画出四等份', json.loads((self.root / 'fraction-add.json').read_text())['task']['current_step'])
        self.assertEqual(0, len(self.app.exception))

    def test_settings_need_separate_confirmation(self):
        self.app.selectbox(key='long-fraction-add-explanation_mode-0').select('direct')
        button(self.app, '生成长期设置建议').click().run()
        self.assertFalse((self.root / 'fraction-add.json').exists())
        button(self.app, '接受更新').click().run()
        saved = json.loads((self.root / 'fraction-add.json').read_text())
        self.assertEqual('direct', saved['settings']['explanation_mode'])
        self.assertEqual('读题，选择需要的帮助', saved['task']['current_step'])
        self.app.run()
        self.assertEqual(1, json.loads((self.root / 'fraction-add.json').read_text())['metadata']['version'])

    def test_no_results_shows_message_without_confirmation(self):
        self.submit('查笔记：量子纠缠')
        self.assertTrue(any('未找到匹配笔记' in x.value for x in self.app.markdown))
        self.assertFalse(any(b.label == '接受更新' for b in self.app.button))

    def test_answer_only_submission_and_next_round_reset(self):
        self.app.text_input(key='answer-fraction-add').set_value('6/8')
        self.submit('')
        workspace = self.app.session_state['workspace']
        result = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertEqual('correct', result.reply.answer_check['status'])
        self.assertEqual('', self.app.text_input(key='answer-fraction-add').value)
        button(self.app, '接受更新').click().run()
        state = json.loads((self.root / 'fraction-add.json').read_text())
        self.assertEqual([], state['task']['completed_steps'])
        self.submit('给个提示')
        next_round = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertIsNone(next_round.reply.answer_check)

    def test_answer_is_checked_for_selected_question(self):
        self.app.selectbox(key='selected_task').select('fraction-word').run()
        self.app.text_input(key='answer-fraction-word').set_value('3/4')
        self.submit('')
        workspace = self.app.session_state['workspace']
        result = workspace.rounds[self.app.session_state['latest']['fraction-word']]
        self.assertEqual('incorrect', result.reply.answer_check['status'])
        self.assertIn('本题问还剩多少', result.reply.explanation)
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_steps_only_submission_rerun_and_reset(self):
        self.app.text_area(key='steps-fraction-add').set_value('1/2=2/4\n2/4+1/4=2/6')
        self.submit('')
        workspace = self.app.session_state['workspace']
        result = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertEqual(2, result.reply.step_check['first_issue'])
        self.assertEqual('', self.app.text_area(key='steps-fraction-add').value)
        self.assertTrue(any('等号两边不相等' in error.value for error in self.app.error))
        self.app.run()
        self.assertEqual(1, workspace.generation_count)
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_variant_steps_are_scoped_to_selected_task(self):
        self.app.selectbox(key='selected_task').select('fraction-add-thirds').run()
        self.app.text_area(key='steps-fraction-add-thirds').set_value('1/3+1/6=1/2')
        self.submit('')
        workspace = self.app.session_state['workspace']
        result = workspace.rounds[self.app.session_state['latest']['fraction-add-thirds']]
        self.assertEqual('verified', result.reply.step_check['status'])
        button(self.app, '接受更新').click().run()
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.assertEqual([], json.loads((self.root / 'fraction-add-thirds.json').read_text())['task']['completed_steps'])

    def test_unknown_explanation_has_no_confirmation_button(self):
        self.app.text_area(key='steps-fraction-add').set_value('我是凭感觉做的')
        self.submit('')
        self.assertFalse(any(b.label == '接受更新' for b in self.app.button))
        self.assertTrue(any('文字解释尚不能自动核对' in row.value for row in self.app.info))

    def test_six_temporary_controls_affect_reply_and_all_reset(self):
        for name, choices in SETTING_OPTIONS.items():
            self.app.selectbox(key=f'temp-fraction-add-{name}').select(choices[-1])
        self.submit('帮我理解')
        workspace = self.app.session_state['workspace']
        first = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertEqual({key: values[-1] for key, values in SETTING_OPTIONS.items()}, first.effective_settings)
        self.assertIn('试着补全', first.reply.explanation)
        self.assertIn('方法提示', first.reply.explanation)
        self.assertIn('数轴', first.reply.explanation)
        for name in SETTING_OPTIONS:
            self.assertEqual('inherit', self.app.selectbox(key=f'temp-fraction-add-{name}').value)
        self.assertFalse((self.root / 'fraction-add.json').exists())
        self.submit('帮我理解')
        second = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertNotIn('试着补全', second.reply.explanation)

    def test_new_long_term_controls_support_reject_edit_and_restart(self):
        updates = {'structure_level': 'guided', 'pattern_guidance': 'on', 'scope_support': 'connected'}
        for name, value in updates.items():
            self.app.selectbox(key=f'long-fraction-add-{name}-0').select(value)
        button(self.app, '生成长期设置建议').click().run()
        self.assertFalse((self.root / 'fraction-add.json').exists())
        button(self.app, '拒绝更新').click().run()
        self.assertFalse((self.root / 'fraction-add.json').exists())
        button(self.app, '生成长期设置建议').click().run()
        proposal = self.app.session_state['settings_pending']['fraction-add']
        self.app.selectbox(key=f'edit-{proposal.proposal_id}-scope_support').select('focused')
        button(self.app, '保存编辑后的更新').click().run()
        self.assertEqual(0, len(self.app.exception))
        before = (self.root / 'fraction-add.json').read_bytes()
        restored = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(0, len(restored.exception))
        self.assertEqual('guided', restored.selectbox(key='long-fraction-add-structure_level-1').value)
        self.assertEqual('focused', restored.selectbox(key='long-fraction-add-scope_support-1').value)
        self.assertEqual(before, (self.root / 'fraction-add.json').read_bytes())
        self.assertFalse((self.root / 'fraction-word.json').exists())

    def test_replay_reply_renders_validated_source_and_still_needs_confirmation(self):
        raw = (APP.parent.parent / 'examples' / 'model_reply.json').read_text()
        self.app.session_state['workspace'] = Workspace(self.root, tutor=ReplayTutor(raw))
        self.submit('查笔记：通分')
        self.assertTrue(any('预设 JSON，无真实 API' in x.value for x in self.app.caption))
        self.assertTrue(any('本轮回复引用：M-F03' in x.value for x in self.app.caption))
        self.assertFalse((self.root / 'fraction-add.json').exists())
        button(self.app, '接受更新').click().run()
        self.assertEqual([], json.loads((self.root / 'fraction-add.json').read_text())['task']['completed_steps'])

    def test_rejected_reply_keeps_local_checks_visible_without_confirmation(self):
        self.app.session_state['workspace'] = Workspace(self.root, tutor=ReplayTutor('not JSON'))
        self.app.text_input(key='answer-fraction-add').set_value('3/4')
        self.app.text_area(key='steps-fraction-add').set_value('1/2=2/6')
        self.submit('')
        self.assertTrue(any('本轮未生成帮助' in x.value for x in self.app.error))
        self.assertTrue(any('最终答案数值相符，但解题步骤中仍有错误' in x.value for x in self.app.info))
        self.assertTrue(any('等号两边不相等' in x.value for x in self.app.error))
        self.assertFalse(any(b.label == '接受更新' for b in self.app.button))
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_unsupported_method_claim_keeps_answer_check_but_hides_suggestion(self):
        value = json.loads((APP.parent.parent / 'examples' / 'model_reply.json').read_text())
        value['explanation'] = 'PRIVATE_METHOD_UI 你的结果 2/6 说明分母也被相加了。'
        value['cited_source_ids'] = []
        self.app.session_state['workspace'] = Workspace(self.root, tutor=ReplayTutor(json.dumps(value)))
        self.app.text_input(key='answer-fraction-add').set_value('2/6')
        button(self.app, '给个小提示').click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any('仅凭答案' in item.value for item in self.app.error))
        self.assertTrue(any('结果还不相符' in item.value for item in self.app.info))
        self.assertFalse(any(item.label == '接受更新' for item in self.app.button))
        self.assertFalse(any('PRIVATE_METHOD_UI' in item.value for item in self.app.text))
        self.assertFalse((self.root / 'fraction-add.json').exists())

    def test_http_result_rerun_and_confirmation_send_only_one_request(self):
        with LocalModelServer() as server:
            tutor = ApiTutor(ModelConfig(mode='api', model='fixture-model'), 'local-test-key', transport=HttpTransport(server.url))
            self.app.session_state['workspace'] = Workspace(self.root, tutor=tutor)
            self.submit('帮助我')
            self.assertTrue(any('本地 HTTP 接口测试' in x.value for x in self.app.caption))
            self.assertEqual(1, len(server.requests))
            self.app.run()
            button(self.app, '接受更新').click().run()
            self.app.run()
            self.assertEqual(1, len(server.requests))
            self.assertEqual(0, len(self.app.exception))
            state = json.loads((self.root / 'fraction-add.json').read_text())
            self.assertEqual(1, state['metadata']['version'])
            self.assertEqual([], state['task']['completed_steps'])

    def test_http_failure_keeps_local_math_check_and_does_not_offer_update(self):
        with LocalModelServer() as server:
            server.status = 429
            tutor = ApiTutor(ModelConfig(mode='api', model='fixture-model'), 'local-test-key', transport=HttpTransport(server.url))
            self.app.session_state['workspace'] = Workspace(self.root, tutor=tutor)
            self.app.text_input(key='answer-fraction-add').set_value('3/4')
            self.submit('')
            self.assertTrue(any('限额或请求频率' in x.value for x in self.app.error))
            self.assertTrue(any('数值正确' in x.value for x in self.app.info))
            self.assertFalse(any(b.label == '接受更新' for b in self.app.button))
            self.app.run()
            self.assertEqual(1, len(server.requests))

    def test_incomplete_api_configuration_stops_without_mock_fallback(self):
        config = self.root / 'model_config.json'
        config.write_text(json.dumps({'mode': 'api', 'provider': 'openai', 'model': 'fixture-model',
                                      'timeout_seconds': 30, 'max_output_tokens': 2000}))
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(0, len(app.exception))
        self.assertTrue(any('缺少模型 API 密钥' in x.value for x in app.error))
        self.assertFalse(any(b.label == '提交求助' for b in app.button))

    def test_help_buttons_work_without_magic_phrases_and_do_not_complete_task(self):
        for label, mode in [('给个小提示', 'hint'), ('完整讲解', 'direct'), ('建议下一步', 'hint')]:
            button(self.app, label).click().run()
            self.assertFalse(self.app.exception)
            workspace = self.app.session_state['workspace']
            result = workspace.rounds[self.app.session_state['latest']['fraction-add']]
            self.assertIsNone(result.error)
            self.assertEqual(mode, result.effective_settings['explanation_mode'])
            self.assertFalse((self.root / 'fraction-add.json').exists())
        button(self.app, '接受更新').click().run()
        state = json.loads((self.root / 'fraction-add.json').read_text())
        self.assertEqual([], state['task']['completed_steps'])

    def test_explicit_note_search_and_sidebar_layout(self):
        self.app.text_input(key='notes-fraction-add').set_value('通分')
        self.submit('')
        workspace = self.app.session_state['workspace']
        result = workspace.rounds[self.app.session_state['latest']['fraction-add']]
        self.assertTrue(result.sources)
        self.assertEqual('通分', result.tool_records[0]['arguments']['query'])
        self.assertEqual('', self.app.text_input(key='notes-fraction-add').value)
        self.assertTrue(any(row.value == '长期设置' for row in self.app.sidebar.subheader))
        self.assertTrue(any(row.value == '已确认进度' for row in self.app.sidebar.subheader))
        self.assertTrue(any(row.label == '本轮设置（可选）' for row in self.app.expander))
        self.assertTrue(any('模拟模式' in row.value for row in self.app.info))

    def test_chat_next_button_accepts_once_and_keeps_reference_answers_collapsed(self):
        with LocalModelServer() as server:
            server.body = chat_envelope
            tutor = ApiTutor(load_model_config(APP.parent.parent / 'model_config.deepseek.example.json'),
                             'local-test-key', transport=HttpTransport(server.chat_url))
            self.app.session_state['workspace'] = Workspace(self.root, tutor=tutor)
            self.app.selectbox(key='temp-fraction-add-explanation_mode').select('direct')
            self.app.text_input(key='notes-fraction-add').set_value('通分')
            button(self.app, '建议下一步').click().run()
            self.assertFalse(self.app.exception)
            self.assertFalse((self.root / 'fraction-add.json').exists())
            context = json.loads(server.requests[0]['payload']['messages'][1]['content'])['context']
            self.assertEqual('next', context['requested_help'])
            self.assertEqual('hint', context['effective_settings']['explanation_mode'])
            notes = next(row for row in self.app.expander if row.label.startswith('参考笔记'))
            self.assertFalse(notes.proto.expanded)
            button(self.app, '接受更新').click().run()
            self.app.run()
            self.assertEqual(1, len(server.requests))
            saved = json.loads((self.root / 'fraction-add.json').read_text())
            self.assertEqual(1, saved['metadata']['version'])
            self.assertEqual([], saved['task']['completed_steps'])

    def test_hint_leak_shows_error_and_local_check_without_reply_or_confirmation(self):
        with LocalModelServer() as server:
            def leak(payload):
                envelope = chat_envelope(payload)
                value = json.loads(envelope['choices'][0]['message']['content'])
                value['optional_hint'] = 'PRIVATE_LEAK_UI 结果是 3/4。'
                envelope['choices'][0]['message']['content'] = json.dumps(value)
                return envelope
            server.body = leak
            tutor = ApiTutor(load_model_config(APP.parent.parent / 'model_config.deepseek.example.json'),
                             'local-test-key', transport=HttpTransport(server.chat_url))
            self.app.session_state['workspace'] = Workspace(self.root, tutor=tutor)
            self.app.text_input(key='answer-fraction-add').set_value('2/6')
            button(self.app, '给个小提示').click().run()
            self.assertFalse(self.app.exception)
            self.assertTrue(any('已拦截' in item.value for item in self.app.error))
            self.assertTrue(any('结果还不相符' in item.value for item in self.app.info))
            self.assertFalse(any(item.label == '接受更新' for item in self.app.button))
            self.assertFalse(any('PRIVATE_LEAK_UI' in item.value for item in self.app.text))
            self.app.run()
            self.assertEqual(1, len(server.requests))
            self.assertFalse((self.root / 'fraction-add.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
