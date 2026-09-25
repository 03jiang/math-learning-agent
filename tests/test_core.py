from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from legacy.core import LearningAssistant, ValidationError


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.app = LearningAssistant(self.path)

    def seed(self):
        self.app.confirm(self.app.propose({"current_step": "检查题意"}))
        return self.path.read_bytes()

    def test_unconfirmed_never_writes(self):
        self.app.simulate_reply()
        self.assertFalse(self.path.exists())
        before = self.seed()
        self.app.simulate_reply()
        self.assertEqual(before, self.path.read_bytes())

    def test_reject_changes_neither_state_nor_file(self):
        original = self.app.snapshot()
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        self.app.reject(p)
        self.assertEqual(original, self.app.snapshot())
        self.assertFalse(self.path.exists())
        before = self.seed()
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        self.app.reject(p)
        with self.assertRaises(ValidationError):
            self.app.confirm(p)
        self.assertEqual(before, self.path.read_bytes())

    def test_legal_update_saved(self):
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        self.assertEqual("applied", self.app.confirm(p))
        data = json.loads(self.path.read_text())
        self.assertEqual("确认两个分数对应同样大小的整体", data["task"]["current_step"])
        self.assertEqual(1, data["metadata"]["version"])

    def test_edit_then_confirm(self):
        _, p = self.app.simulate_reply()
        self.app.confirm(p, edited_state={"current_step": "画出四等份长条"})
        self.assertEqual("画出四等份长条", self.app.snapshot().task.current_step)

    def test_repeat_confirmation_even_after_restart(self):
        p = self.app.propose({"completed_steps": ["读题", "确认两个分数对应同样大小的整体"]})
        self.app.confirm(p)
        before = self.path.read_bytes()
        self.assertEqual("already_applied", self.app.confirm(p))
        self.assertEqual("already_applied", LearningAssistant(self.path).confirm(p))
        self.assertEqual(before, self.path.read_bytes())

    def test_accept_next_step_does_not_complete_it(self):
        completed = self.app.snapshot().task.completed_steps
        _, p = self.app.simulate_reply()
        self.app.confirm(p)
        self.assertEqual(completed, self.app.snapshot().task.completed_steps)
        self.assertEqual("确认两个分数对应同样大小的整体", self.app.snapshot().task.current_step)

    def test_illegal_patches_never_write(self):
        before = self.seed()
        for invalid in [{"profile": "初学者"}, {"version": 99}, {"current_step": 3},
                        {"current_step": " "}, {"completed_steps": "完成"},
                        {"requirements": [1]}, {}, ["current_step"]]:
            with self.subTest(invalid=invalid):
                p = self.app.propose(invalid)
                with self.assertRaises(ValidationError):
                    self.app.confirm(p)
                self.assertEqual(before, self.path.read_bytes())

    def test_invalid_edit_never_writes(self):
        before = self.seed()
        _, p = self.app.simulate_reply()
        with self.assertRaises(ValidationError):
            self.app.confirm(p, edited_state={"current_step": ""})
        self.assertEqual(before, self.path.read_bytes())

    def test_stale_version_rejected_on_disk_reload(self):
        p = self.app.propose({"current_step": "旧建议"})
        other = LearningAssistant(self.path)
        other.confirm(other.propose({"current_step": "新步骤"}))
        before = self.path.read_bytes()
        with self.assertRaises(ValidationError):
            self.app.confirm(p)
        self.assertEqual(before, self.path.read_bytes())

    def test_expired_suggestion_rejected(self):
        before = self.seed()
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        with patch("legacy.core.time.time", return_value=p.expires_at):
            with self.assertRaises(ValidationError):
                self.app.confirm(p)
        self.assertEqual(before, self.path.read_bytes())

    def test_tampered_or_unknown_suggestion_rejected(self):
        before = self.seed()
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        for changed in [replace(p, task_id="another-task"), replace(p, proposal_id="forged"),
                        replace(p, base_version=999)]:
            with self.assertRaises(ValidationError):
                self.app.confirm(changed)
        p.proposed_state_update["current_step"] = "擅自改写"
        with self.assertRaises(ValidationError):
            self.app.confirm(p)
        self.assertEqual(before, self.path.read_bytes())

    def test_settings_require_confirmation_and_restore(self):
        p = self.app.propose(configuration_patch={"explanation_mode": "direct", "presentation_density": "detailed"})
        self.assertEqual("hint", self.app.snapshot().settings.explanation_mode)
        self.assertFalse(self.path.exists())
        self.app.confirm(p)
        restored = LearningAssistant(self.path)
        self.assertEqual("direct", restored.snapshot().settings.explanation_mode)
        reply, _ = restored.simulate_reply()
        self.assertIn("1/2 = 2/4", reply.explanation)
        self.assertIn("统一分母", reply.explanation)

    def test_settings_and_state_validate_as_one_transaction(self):
        before = self.seed()
        for config in [{"step_size": "huge"}, {"structure_level": "high"}, {"step_size": []}]:
            p = self.app.propose({"current_step": "不能部分保存"}, config)
            with self.assertRaises(ValidationError):
                self.app.confirm(p)
            self.assertEqual(before, self.path.read_bytes())

    def test_save_failure_keeps_old_data_and_allows_retry(self):
        before = self.seed()
        p = self.app.propose({"current_step": "确认两个分数对应同样大小的整体"})
        for target in ["legacy.core.os.fsync", "legacy.core.os.replace"]:
            with patch(target, side_effect=OSError("模拟磁盘写入失败")):
                with self.assertRaises(OSError):
                    self.app.confirm(p)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual([], list(self.path.parent.glob(".state-*.tmp")))
        self.assertEqual("applied", self.app.confirm(p))

    def test_restart_in_separate_python_process(self):
        self.app.confirm(self.app.propose({"current_step": "确认两个分数对应同样大小的整体"}, {"step_size": "medium"}))
        result = subprocess.run(
            [sys.executable, "-c", "from legacy.core import LearningAssistant; from dataclasses import asdict; "
             "import json,sys; print(json.dumps(asdict(LearningAssistant(sys.argv[1]).snapshot())))", str(self.path)],
            cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True)
        self.assertEqual(asdict(self.app.snapshot()), json.loads(result.stdout))

    def test_unconfirmed_proposals_are_discarded_on_restart(self):
        p = self.app.propose({"current_step": "未确认步骤"})
        with self.assertRaises(ValidationError):
            LearningAssistant(self.path).confirm(p)
        self.assertFalse(self.path.exists())

    def test_corrupt_file_is_not_overwritten(self):
        for content in ["broken JSON", '{"settings": {}}']:
            self.path.write_text(content)
            with self.assertRaises(ValueError):
                LearningAssistant(self.path)
            self.assertEqual(content, self.path.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
