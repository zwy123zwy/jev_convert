"""S1 之后各阶段之间传递的中间结构。

S1 产出 CaseDraft（在 profile.py），S2 产出 NormalizedCase，S3 产出 GroundedCase，
S4 把 GroundedCase 变成 plan.Plan。中间结构集中在这里，避免各阶段各自造 shape。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jev_convert.compile.plan import KINDS, PlanError, Target

EVIDENCE_VALUES = ("archive", "source", "trial")


class DraftError(PlanError):
    """中间结构字段不合法。"""


def _fields(data: Mapping[str, Any], expected: tuple[str, ...], label: str, optional: tuple[str, ...] = ()) -> None:
    missing = [key for key in expected if key not in data]
    if missing:
        raise DraftError(f"{label} 缺少字段：{', '.join(missing)}")
    unknown = [key for key in data if key not in expected and key not in optional]
    if unknown:
        raise DraftError(f"{label} 出现未知字段：{', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class NormalizedStep:
    source_node: str
    original: str
    text: str
    expect: tuple[str, ...] = ()
    kind: str = "click"

    def to_dict(self) -> dict[str, Any]:
        if self.kind not in KINDS:
            raise DraftError(f"NormalizedStep.kind 不在枚举内：{self.kind!r}")
        return {"source_node": self.source_node, "original": self.original, "text": self.text,
                "expect": list(self.expect), "kind": self.kind}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NormalizedStep:
        _fields(data, ("source_node", "original", "text", "expect", "kind"), "NormalizedStep")
        step = cls(source_node=data["source_node"], original=data["original"], text=data["text"],
                   expect=tuple(data["expect"]), kind=data["kind"])
        step.to_dict()
        return step


@dataclass(frozen=True)
class NormalizedCase:
    source_node: str
    title: str
    menu: tuple[str, ...]
    preconditions: tuple[str, ...]
    steps: tuple[NormalizedStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"source_node": self.source_node, "title": self.title, "menu": list(self.menu),
                "preconditions": list(self.preconditions),
                "steps": [step.to_dict() for step in self.steps]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NormalizedCase:
        _fields(data, ("source_node", "title", "menu", "preconditions", "steps"), "NormalizedCase")
        return cls(source_node=data["source_node"], title=data["title"], menu=tuple(data["menu"]),
                   preconditions=tuple(data["preconditions"]),
                   steps=tuple(NormalizedStep.from_dict(item) for item in data["steps"]))


@dataclass(frozen=True)
class GroundedStep:
    seq: int
    source_node: str
    original: str
    page: str
    kind: str
    goal: str
    target: Target
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    expect: tuple[str, ...] = ()
    # 这一步执行后应到达的页面节点 id（由本步预期里的「进入 / 跳转到 / 返回」判断）；不换页时为空。
    destination: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.kind not in KINDS:
            raise DraftError(f"GroundedStep.kind 不在枚举内：{self.kind!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise DraftError(f"GroundedStep.confidence 必须在 [0, 1]，实际是 {self.confidence}")
        for item in self.evidence:
            if item not in EVIDENCE_VALUES:
                raise DraftError(f"GroundedStep.evidence 不在枚举内：{item!r}")
        return {"seq": self.seq, "source_node": self.source_node, "original": self.original,
                "page": self.page, "kind": self.kind, "goal": self.goal, "target": self.target.to_dict(),
                "confidence": self.confidence, "evidence": list(self.evidence), "expect": list(self.expect),
                "destination": self.destination}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GroundedStep:
        """旧数据没有 destination 字段时按 "" 处理。"""
        _fields(data, ("seq", "source_node", "original", "page", "kind", "goal", "target", "confidence",
                       "evidence", "expect"), "GroundedStep", optional=("destination",))
        destination = data.get("destination", "")
        if not isinstance(destination, str):
            raise DraftError(f"GroundedStep.destination 必须是字符串，实际是 {type(destination).__name__}")
        step = cls(seq=data["seq"], source_node=data["source_node"], original=data["original"],
                   page=data["page"], kind=data["kind"], goal=data["goal"],
                   target=Target.from_dict(data["target"]), confidence=data["confidence"],
                   evidence=tuple(data["evidence"]), expect=tuple(data["expect"]), destination=destination)
        step.to_dict()
        return step


@dataclass(frozen=True)
class GroundedCase:
    source_node: str
    title: str
    menu: tuple[str, ...]
    requires: tuple[str, ...]
    entry_node: str
    kb_version: str
    steps: tuple[GroundedStep, ...]
    not_automatable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"source_node": self.source_node, "title": self.title, "menu": list(self.menu),
                "requires": list(self.requires), "entry_node": self.entry_node,
                "kb_version": self.kb_version, "not_automatable": self.not_automatable,
                "steps": [step.to_dict() for step in self.steps]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GroundedCase:
        _fields(data, ("source_node", "title", "menu", "requires", "entry_node", "kb_version",
                       "not_automatable", "steps"), "GroundedCase")
        return cls(source_node=data["source_node"], title=data["title"], menu=tuple(data["menu"]),
                   requires=tuple(data["requires"]), entry_node=data["entry_node"],
                   kb_version=data["kb_version"], not_automatable=data["not_automatable"],
                   steps=tuple(GroundedStep.from_dict(item) for item in data["steps"]))
