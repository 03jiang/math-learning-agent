"""第一阶段：单用户、单写入进程的本地确认流程，仅使用 Python 标准库。"""
from dataclasses import asdict, dataclass, field
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4


class ValidationError(ValueError):
    pass


SETTING_OPTIONS = {
    "step_size": ("small", "medium", "large"),
    "explanation_mode": ("hint", "example", "direct"),
    "presentation_density": ("brief", "detailed"),
    "structure_level": ("free", "guided"),
    "pattern_guidance": ("off", "on"),
    "scope_support": ("focused", "connected"),
}
LEGACY_SETTING_FIELDS = {"step_size", "explanation_mode", "presentation_density"}
STATE_FIELDS = {"objective", "requirements", "current_step", "completed_steps", "deferred_ideas"}


@dataclass
class LearningSettings:
    step_size: str = "small"
    explanation_mode: str = "hint"
    presentation_density: str = "brief"
    structure_level: str = "free"
    pattern_guidance: str = "off"
    scope_support: str = "focused"


@dataclass
class TaskState:
    objective: str = "理解并计算 1/2 + 1/4"
    requirements: list[str] = field(default_factory=lambda: ["确认同一个整体", "先统一分数单位再相加"])
    current_step: str = "读题，选择需要的帮助"
    completed_steps: list[str] = field(default_factory=list)
    deferred_ideas: list[str] = field(default_factory=list)


@dataclass
class Metadata:
    task_id: str = "fraction-add"
    version: int = 0  # 任一已确认设置或任务更新均推进版本
    applied_proposal_ids: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    settings: LearningSettings = field(default_factory=LearningSettings)
    task: TaskState = field(default_factory=TaskState)
    metadata: Metadata = field(default_factory=Metadata)


@dataclass(frozen=True)
class PendingUpdate:
    proposal_id: str
    task_id: str
    base_version: int
    expires_at: float
    proposed_state_update: dict | None = None
    proposed_configuration_update: dict | None = None


@dataclass(frozen=True)
class TutorReply:
    explanation: str
    next_action: str
    optional_hint: str | None
    proposed_state_update: dict | None
    proposed_configuration_update: dict | None
    source: str = "模拟回复（固定规则，无真实模型）"
    answer_check: dict | None = None
    step_check: dict | None = None
    cited_source_ids: list[str] = field(default_factory=list)
    origin: str = 'rule_mock'


SimulatedReply = TutorReply  # 保留早期示例的导入名称，存档格式不变


def validate_patch(patch, allowed, settings=False):
    if type(patch) is not dict or not patch or not patch.keys() <= allowed:
        raise ValidationError("更新必须是非空字典，且只能包含允许的字段")
    for key, value in patch.items():
        if settings:
            if type(value) is not str or value not in SETTING_OPTIONS[key]:
                raise ValidationError(f"非法设置：{key}")
        elif key in {"objective", "current_step"}:
            if type(value) is not str or not value.strip():
                raise ValidationError(f"{key} 必须是非空字符串")
        elif type(value) is not list or any(type(x) is not str or not x.strip() for x in value):
            raise ValidationError(f"{key} 必须是非空字符串组成的列表（允许空列表）")


def decode_snapshot(data):
    """只读兼容完整旧三项设置；其他缺失或损坏报错，不自动写回。"""
    if type(data) is not dict or set(data) != {"settings", "task", "metadata"}:
        raise ValidationError("无效存档结构")
    validate_patch(data["settings"], SETTING_OPTIONS.keys(), settings=True)
    validate_patch(data["task"], STATE_FIELDS)
    if (set(data["settings"]) not in (LEGACY_SETTING_FIELDS, set(SETTING_OPTIONS))
            or set(data["task"]) != STATE_FIELDS):
        raise ValidationError("存档字段不完整")
    meta = data["metadata"]
    if type(meta) is not dict or set(meta) != {"task_id", "version", "applied_proposal_ids"}:
        raise ValidationError("无效元数据")
    ids = meta["applied_proposal_ids"]
    if (type(meta["task_id"]) is not str or not meta["task_id"].strip()
            or type(meta["version"]) is not int or meta["version"] < 0
            or type(ids) is not list or any(type(x) is not str or not x for x in ids)
            or len(ids) != len(set(ids))):
        raise ValidationError("无效元数据取值")
    # dataclass 只为已识别的旧格式补三个默认值；原字典、文件、版本均不改。
    return Snapshot(LearningSettings(**data["settings"]), TaskState(**data["task"]), Metadata(**meta))


class JsonStore:
    def __init__(self, path, initial=None):
        self.path = Path(path)
        self.initial = deepcopy(initial or Snapshot())

    def load(self):
        if not self.path.exists():
            return deepcopy(self.initial)  # 只读默认值；尚未确认时不创建文件
        result = decode_snapshot(json.loads(self.path.read_text(encoding="utf-8")))
        if result.metadata.task_id != self.initial.metadata.task_id:
            raise ValidationError("存档任务 ID 与当前任务不匹配")
        return result

    def save(self, snapshot):
        data = asdict(snapshot)
        decode_snapshot(data)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".state-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)  # 同目录替换：新文件完整写好后才替换旧文件
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)


class LearningAssistant:
    def __init__(self, path, initial=None):
        self.store = JsonStore(path, initial)
        self.store.load()  # 及早发现损坏存档
        self._pending = {}

    def snapshot(self):
        return self.store.load()

    def propose(self, state_patch=None, configuration_patch=None):
        """模拟模型只提交建议；此处不保存，也不将其视为可信数据。"""
        meta = self.snapshot().metadata
        proposal = PendingUpdate(str(uuid4()), meta.task_id, meta.version, time.time() + 900,
                                 deepcopy(state_patch), deepcopy(configuration_patch))
        self._pending[proposal.proposal_id] = deepcopy(proposal)
        return proposal

    def reject(self, proposal):
        if self._pending.get(proposal.proposal_id) != proposal:
            raise ValidationError("未知、已处理或被篡改的建议")
        del self._pending[proposal.proposal_id]  # 拒绝只丢弃内存建议，不写 JSON

    def confirm(self, proposal, *, edited_state=None, edited_configuration=None):
        current = self.snapshot()  # 确认时重读，避免用旧快照覆盖新状态
        meta = current.metadata
        if proposal.proposal_id in meta.applied_proposal_ids:
            return "already_applied"
        if self._pending.get(proposal.proposal_id) != proposal:
            raise ValidationError("未知、已拒绝或被篡改的建议，请重新生成")
        if proposal.task_id != meta.task_id or proposal.base_version != meta.version:
            raise ValidationError("建议任务不匹配或版本已过期")
        if time.time() >= proposal.expires_at:
            raise ValidationError("建议已超过 15 分钟，请重新生成")
        state_patch = proposal.proposed_state_update if edited_state is None else edited_state
        config_patch = proposal.proposed_configuration_update if edited_configuration is None else edited_configuration
        if state_patch is None and config_patch is None:
            raise ValidationError("没有可确认的更新")
        candidate = deepcopy(current)
        for patch, target, allowed, is_setting in (
            (state_patch, candidate.task, STATE_FIELDS, False),
            (config_patch, candidate.settings, SETTING_OPTIONS.keys(), True),
        ):
            if patch is not None:
                validate_patch(patch, allowed, is_setting)
                for key, value in patch.items():
                    setattr(target, key, deepcopy(value))
        candidate.metadata.version += 1
        candidate.metadata.applied_proposal_ids.append(proposal.proposal_id)
        self.store.save(candidate)  # 状态、设置、去重 ID 一次保存；失败时建议仍可重试
        del self._pending[proposal.proposal_id]
        return "applied"

    def simulate_reply(self):
        from curriculum import MockTutor
        snapshot = self.snapshot()
        reply = MockTutor().answer('请给一个小提示', snapshot, snapshot.settings, [], False)
        proposal = self.propose(reply.proposed_state_update, reply.proposed_configuration_update) if reply.proposed_state_update else None
        return reply, proposal
