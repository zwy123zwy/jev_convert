"""执行计划的 dataclass、校验与稳定编号（第 4.1 节）。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

STATUSES = ("draft", "confirmed", "rejected", "stale")
KINDS = ("click", "type", "select", "navigate", "wait")
# Step.budget 只为兼容旧计划保留；新执行层每个动作就是一次操作，不再有预算上限（docs/agent-design.md 12.4）。

_KB_VERSION = re.compile(r"^webui@[0-9A-Za-z]+(-dirty)?$")
_SEPARATORS = re.compile(r"[^0-9A-Za-z]+")
_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")
_MISSING = object()
_DATA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_DATA_NAMES = ("data", "account")


class PlanError(ValueError):
    """执行计划不合法：字段、枚举、序号或预算有问题。"""


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"{where} 必须是 JSON 对象，实际是 {type(value).__name__}")
    return value


def _check_fields(
    data: Mapping[str, Any], where: str, required: Sequence[str], optional: Sequence[str] = ()
) -> None:
    unknown = sorted(set(data) - set(required) - set(optional))
    if unknown:
        raise PlanError(f"{where} 含未知字段：{', '.join(unknown)}")
    missing = [name for name in required if name not in data]
    if missing:
        raise PlanError(f"{where} 缺少必填字段：{', '.join(missing)}")


def _text(data: Mapping[str, Any], where: str, key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise PlanError(f"{where}.{key} 必须是字符串，实际是 {type(value).__name__}")
    return value


def _texts(data: Mapping[str, Any], where: str, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return default
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise PlanError(f"{where}.{key} 必须是字符串列表")
    return tuple(value)


def _integer(data: Mapping[str, Any], where: str, key: str, default: int = 0) -> int:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(f"{where}.{key} 必须是整数，实际是 {type(value).__name__}")
    return value


def _number(data: Mapping[str, Any], where: str, key: str, default: float = 0.0) -> float:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{where}.{key} 必须是数字，实际是 {type(value).__name__}")
    return float(value)


def _flag(data: Mapping[str, Any], where: str, key: str, default: bool) -> bool:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        raise PlanError(f"{where}.{key} 必须是布尔值，实际是 {type(value).__name__}")
    return value


@dataclass(frozen=True)
class SourceRef:
    mindnote: str
    node_id: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {"mindnote": self.mindnote, "node_id": self.node_id, "version": self.version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceRef:
        where = "source"
        body = _mapping(data, where)
        _check_fields(body, where, ("mindnote", "node_id", "version"))
        return cls(
            mindnote=_text(body, where, "mindnote"),
            node_id=_text(body, where, "node_id"),
            version=_text(body, where, "version"),
        )


@dataclass(frozen=True)
class Target:
    name: str
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    not_: tuple[str, ...] = ()
    # 运行中确认过的元素 key（docs/agent-design.md 7.2 第 1 级）；空表示还没有确认过。
    locator: str = ""
    # type / select 要填的值；可以写 ${data.x}、${account.x}（凭据只在动作层替换）。
    value: str = ""
    # 目标所在的表格行：行里要包含这段文字，例如 ${task.任务名称}。按数据身份找行，不按行号。
    row: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "not": list(self.not_),
        }
        # 新字段只在有值时写出：旧计划的文件内容不变，编译快照测试不受影响。
        if self.locator:
            data["locator"] = self.locator
        if self.value:
            data["value"] = self.value
        if self.row:
            data["row"] = self.row
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Target:
        where = "target"
        body = _mapping(data, where)
        _check_fields(body, where, ("name",), ("confidence", "evidence", "not", "locator", "value", "row"))
        return cls(
            name=_text(body, where, "name"),
            confidence=_number(body, where, "confidence"),
            evidence=_texts(body, where, "evidence"),
            not_=_texts(body, where, "not"),
            locator=str(body.get("locator", "")),
            value=str(body.get("value", "")),
            row=str(body.get("row", "")),
        )


@dataclass(frozen=True)
class Entry:
    node: str
    route: str
    url: str
    arrive: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "route": self.route, "url": self.url, "arrive": list(self.arrive)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Entry:
        where = "entry"
        body = _mapping(data, where)
        _check_fields(body, where, ("node", "route", "url"), ("arrive",))
        return cls(
            node=_text(body, where, "node"),
            route=_text(body, where, "route"),
            url=_text(body, where, "url"),
            arrive=_texts(body, where, "arrive"),
        )


@dataclass(frozen=True)
class Step:
    seq: int
    source_node: str
    original: str
    page: str
    kind: str
    goal: str
    target: Target
    expect: tuple[str, ...]
    budget: int
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "source_node": self.source_node,
            "original": self.original,
            "page": self.page,
            "kind": self.kind,
            "goal": self.goal,
            "target": self.target.to_dict(),
            "expect": list(self.expect),
            "budget": self.budget,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        where = "step"
        body = _mapping(data, where)
        _check_fields(
            body,
            where,
            ("seq", "source_node", "original", "page", "kind", "goal", "target", "expect", "budget", "status"),
        )
        return cls(
            seq=_integer(body, where, "seq"),
            source_node=_text(body, where, "source_node"),
            original=_text(body, where, "original"),
            page=_text(body, where, "page"),
            kind=_text(body, where, "kind"),
            goal=_text(body, where, "goal"),
            target=Target.from_dict(body["target"]),
            expect=_texts(body, where, "expect"),
            budget=_integer(body, where, "budget"),
            status=_text(body, where, "status"),
        )


@dataclass(frozen=True)
class DataNeed:
    """用例需要的一条测试数据（docs/agent-design.md 第 17 节）。

    计划里用 ${<名字>.<列名>} 引用领到的数据，例如 ${task.任务名称}。
    """

    source: str                       # [data_sources] 里的数据源名
    state: str = ""                   # 需要的状态；空表示任意状态
    consume: bool = False             # 用例会改变这条数据：独占，用完不再分给别的用例
    # 耦合条件：同组（group_column 相同）其他数据要满足的条件，写成“状态=处理中”（同组至少一条）
    # 或“状态!=处理中”（同组一条都没有）。不满足时换一条数据。
    with_group: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source}
        if self.state:
            data["state"] = self.state
        if self.consume:
            data["consume"] = True
        if self.with_group:
            data["with_group"] = list(self.with_group)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DataNeed:
        where = "data"
        body = _mapping(data, where)
        _check_fields(body, where, ("source",), ("state", "consume", "with_group"))
        return cls(
            source=_text(body, where, "source"),
            state=str(body.get("state", "")),
            consume=_flag(body, where, "consume", False),
            with_group=_texts(body, where, "with_group"),
        )


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    status: str
    source: SourceRef
    kb_version: str
    requires: tuple[str, ...]
    entry: Entry
    steps: tuple[Step, ...]
    teardown: tuple[Step, ...] = ()
    destructive: bool = False
    automatable: bool = True
    # 用例需要的测试数据（名字 → 需求），以及跑通后这些数据进入的状态（名字 → 状态）。
    data: Mapping[str, DataNeed] = field(default_factory=dict)
    produces: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = self._body()
        # 新字段只在有值时写出：旧计划的文件内容不变。
        if self.data:
            body["data"] = {name: need.to_dict() for name, need in sorted(self.data.items())}
        if self.produces:
            body["produces"] = dict(sorted(self.produces.items()))
        return body

    def _body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "source": self.source.to_dict(),
            "kb_version": self.kb_version,
            "requires": list(self.requires),
            "entry": self.entry.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "teardown": [step.to_dict() for step in self.teardown],
            "destructive": self.destructive,
            "automatable": self.automatable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Plan:
        where = "plan"
        body = _mapping(data, where)
        _check_fields(
            body,
            where,
            ("id", "title", "status", "source", "kb_version", "requires", "entry", "steps"),
            ("teardown", "destructive", "automatable", "data", "produces"),
        )
        data = body.get("data", {})
        produces = body.get("produces", {})
        if not isinstance(data, Mapping) or not isinstance(produces, Mapping):
            raise PlanError(f"{where}.data 和 {where}.produces 必须是 JSON 对象")
        return cls(
            id=_text(body, where, "id"),
            title=_text(body, where, "title"),
            status=_text(body, where, "status"),
            source=SourceRef.from_dict(body["source"]),
            kb_version=_text(body, where, "kb_version"),
            requires=_texts(body, where, "requires"),
            entry=Entry.from_dict(body["entry"]),
            steps=_steps(body, where, "steps"),
            teardown=_steps(body, where, "teardown"),
            destructive=_flag(body, where, "destructive", False),
            automatable=_flag(body, where, "automatable", True),
            data={str(name): DataNeed.from_dict(need) for name, need in data.items()},
            produces={str(name): str(state) for name, state in produces.items()},
        )


def _steps(data: Mapping[str, Any], where: str, key: str) -> tuple[Step, ...]:
    value = data.get(key, _MISSING)
    if value is _MISSING:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PlanError(f"{where}.{key} 必须是字符串列表形式的对象数组")
    return tuple(Step.from_dict(item) for item in value)


def _validate_step(step: Step, where: str) -> None:
    if step.kind not in KINDS:
        raise PlanError(f"{where}.kind 必须是 {KINDS} 之一，实际是 {step.kind!r}")
    if step.status not in STATUSES:
        raise PlanError(f"{where}.status 必须是 {STATUSES} 之一，实际是 {step.status!r}")
    if not step.expect:
        raise PlanError(f"{where}.expect 不能为空")
    if not step.target.name.strip():
        raise PlanError(f"{where}.target.name 不能为空")
    if not 0.0 <= step.target.confidence <= 1.0:
        raise PlanError(f"{where}.target.confidence 必须在 0 到 1 之间，实际是 {step.target.confidence}")
    if step.budget < 0:
        raise PlanError(f"{where}.budget 不能为负数，实际是 {step.budget}")
    if not step.source_node.strip():
        raise PlanError(f"{where}.source_node 不能为空")


def validate(plan: Plan) -> None:
    """检查执行计划的硬性约束，不合法抛 PlanError（信息里写明哪一项）。"""
    if not isinstance(plan, Plan):
        raise PlanError(f"validate 需要 Plan，实际是 {type(plan).__name__}")
    if not plan.id.startswith("TC-"):
        raise PlanError(f"计划 id 必须以 TC- 开头，实际是 {plan.id!r}")
    if not plan.title.strip():
        raise PlanError("计划 title 不能为空")
    if plan.status not in STATUSES:
        raise PlanError(f"计划 status 必须是 {STATUSES} 之一，实际是 {plan.status!r}")
    if not plan.source.node_id.strip():
        raise PlanError("计划 source.node_id 不能为空")
    if not _KB_VERSION.match(plan.kb_version):
        raise PlanError(f"kb_version 必须形如 webui@<sha>，实际是 {plan.kb_version!r}")
    if not plan.entry.node.strip():
        raise PlanError("计划 entry.node 不能为空")
    if not plan.entry.url.strip():
        raise PlanError("计划 entry.url 不能为空")
    if not plan.steps:
        raise PlanError("计划至少要有一个步骤")
    for name in plan.data:
        if not _DATA_NAME.match(name) or name in RESERVED_DATA_NAMES:
            raise PlanError(f"data 的名字 {name!r} 不合法：只能用字母、数字、下划线，且不能是 {RESERVED_DATA_NAMES}")
    unknown = sorted(set(plan.produces) - set(plan.data))
    if unknown:
        raise PlanError(f"produces 引用了没有声明的数据：{', '.join(unknown)}")

    for position, step in enumerate(plan.steps, start=1):
        if step.seq != position:
            raise PlanError(f"steps 的 seq 必须从 1 连续递增，第 {position} 步的 seq 是 {step.seq}")
        _validate_step(step, f"steps[{position}]")
    for position, step in enumerate(plan.teardown, start=1):
        _validate_step(step, f"teardown[{position}]")



def _ascii_words(text: str) -> list[str]:
    words: list[str] = []
    for chunk in _SEPARATORS.split(text):
        for word in _WORD_BOUNDARY.split(chunk):
            if word:
                words.append(word.lower())
    return words


def make_id(menu: Sequence[str], index: int) -> str:
    """由菜单路径生成稳定短 id：取最后一段里的 ASCII 单词，去掉中文。"""
    if index < 1:
        raise PlanError(f"用例序号从 1 开始，实际是 {index}")
    for segment in reversed([str(item) for item in menu]):
        words = _ascii_words(segment)
        if words:
            return f"TC-{'-'.join(words)}-{index:03d}"
    return f"TC-case-{index:03d}"


def _node_tail(node_id: str) -> str:
    parts = [part for part in str(node_id).split("_") if part]
    if not parts:
        raise PlanError(f"页面节点 id 为空：{node_id!r}")
    return "-".join(parts[-3:])


def make_id_from_node(node_id: str, index: int) -> str:
    """由页面节点 id 生成短 id：取最后三段改成 kebab，再接序号。

    旧接口，保留兼容；序号会随导图里用例的增删而漂移，新代码用 make_case_id。
    """
    if index < 1:
        raise PlanError(f"用例序号从 1 开始，实际是 {index}")
    return f"TC-{_node_tail(node_id)}-{index:03d}"


def make_case_id(entry_node: str, source_node_id: str) -> str:
    """用例 id：TC-<入口节点 id 最后三段，kebab>-<sha1(导图节点 id) 前 6 位十六进制>。

    只由入口页和导图节点 id 决定：同一个 source.node_id 永远得到同一个 id，
    在导图里插入或删除别的用例不会改到它，同页的不同用例也不会撞号。
    """
    source = str(source_node_id).strip()
    if not source:
        raise PlanError(f"导图节点 id 为空：{source_node_id!r}")
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:6]
    return f"TC-{_node_tail(entry_node)}-{digest}"
