"""为导航图里的每个页面节点生成初版知识卡。

对应 docs/implementation.md 第 4.3 节：必填字段 node、structure_version、elements、actions、
confusables、assertions，每个元素与动作都带 source（source=源码）和 verified=0。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Mapping, Sequence

from jev_convert.kb.navgraph import DYNAMIC, NavGraph, NavNode, is_dynamic

SOURCE = "source"
MAX_CONFUSABLES = 8
CONFUSABLE_RATIO = 0.75

# vue.mjs 的抽取桶 → 知识卡里的元素角色与标签字段。
ELEMENT_BUCKETS = (
    ("buttons", "text", "button"),
    ("form_items", "label", "form-item"),
    ("table_columns", "label", "column"),
    ("tabs", "label", "tab"),
    ("dialogs", "title", "dialog"),
)


class CardError(ValueError):
    """知识卡数据不合法。"""


def _require(data: Mapping[str, Any], expected: Sequence[str], label: str, optional: Sequence[str] = ()) -> None:
    if not isinstance(data, Mapping):
        raise CardError(f"{label} 不是对象：{data!r}")
    missing = [name for name in expected if name not in data]
    extra = [name for name in data if name not in expected and name not in optional]
    if missing or extra:
        raise CardError(f"{label} 字段不匹配：缺少 {missing}，多余 {extra}")


def label_text(label: Any) -> str:
    """知识卡里用的稳定排序键：字面量取原文，dynamic 取源码片段的标记形式。"""
    if isinstance(label, str):
        return label
    if is_dynamic(label):
        return "{" + DYNAMIC + "} " + label[DYNAMIC]
    return json.dumps(label, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class CardElement:
    role: str
    label: Any
    source: str = SOURCE
    verified: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "label": self.label, "source": self.source, "verified": self.verified}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CardElement:
        _require(data, ("role", "label", "source", "verified"), "CardElement")
        return cls(role=data["role"], label=data["label"], source=data["source"], verified=data["verified"])


@dataclass(frozen=True)
class CardAction:
    kind: str
    target: Any
    source: str = SOURCE
    verified: int = 0
    level: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "source": self.source, "verified": self.verified,
                "level": self.level}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CardAction:
        _require(data, ("kind", "target", "source", "verified", "level"), "CardAction")
        return cls(kind=data["kind"], target=data["target"], source=data["source"], verified=data["verified"],
                   level=data["level"])


@dataclass(frozen=True)
class CardAssertion:
    kind: str
    value: Any
    source: str = SOURCE
    verified: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "source": self.source, "verified": self.verified}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CardAssertion:
        _require(data, ("kind", "value", "source", "verified"), "CardAssertion")
        return cls(kind=data["kind"], value=data["value"], source=data["source"], verified=data["verified"])


@dataclass(frozen=True)
class Confusable:
    a: str
    b: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Confusable:
        _require(data, ("a", "b", "reason"), "Confusable")
        return cls(a=data["a"], b=data["b"], reason=data["reason"])


@dataclass(frozen=True)
class PageCard:
    node: str
    structure_version: str
    path: str
    title: str = ""
    menu: tuple[str, ...] = ()
    required_query: tuple[str, ...] = ()
    elements: tuple[CardElement, ...] = ()
    actions: tuple[CardAction, ...] = ()
    confusables: tuple[Confusable, ...] = ()
    assertions: tuple[CardAssertion, ...] = ()
    dynamic_labels: int = 0
    # 起点契约（docs/agent-design.md 6.2）：route / context / landmarks / reset。
    # 由 jev-convert kb learn 从通过的运行里写入，源码抽取不生成；空表示还没有确认过的起点。
    start: Mapping[str, Any] = dc_field(default_factory=dict)
    # 别名：导图里的说法（规范化后）→ 真实元素名。来自确认表（Step 13），加载时由 confirmed.json 叠加。
    aliases: Mapping[str, str] = dc_field(default_factory=dict)

    @property
    def filename(self) -> str:
        version = self.structure_version.replace("/", "_").replace("\\", "_")
        return f"{self.node}__{version}.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "structure_version": self.structure_version,
            "path": self.path,
            "title": self.title,
            "menu": list(self.menu),
            "required_query": list(self.required_query),
            "elements": [element.to_dict() for element in self.elements],
            "actions": [action.to_dict() for action in self.actions],
            "confusables": [item.to_dict() for item in self.confusables],
            "assertions": [assertion.to_dict() for assertion in self.assertions],
            "dynamic_labels": self.dynamic_labels,
            **({"start": dict(self.start)} if self.start else {}),
            **({"aliases": dict(sorted(self.aliases.items()))} if self.aliases else {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PageCard:
        _require(data, ("node", "structure_version", "path", "title", "menu", "required_query", "elements", "actions",
                        "confusables", "assertions", "dynamic_labels"), "PageCard", optional=("start", "aliases"))
        return cls(
            node=data["node"],
            structure_version=data["structure_version"],
            path=data["path"],
            title=data["title"],
            menu=tuple(data["menu"]),
            required_query=tuple(data["required_query"]),
            elements=tuple(CardElement.from_dict(item) for item in data["elements"]),
            actions=tuple(CardAction.from_dict(item) for item in data["actions"]),
            confusables=tuple(Confusable.from_dict(item) for item in data["confusables"]),
            assertions=tuple(CardAssertion.from_dict(item) for item in data["assertions"]),
            dynamic_labels=data["dynamic_labels"],
            start=dict(data.get("start") or {}),
            aliases={str(key): str(value) for key, value in (data.get("aliases") or {}).items()},
        )


def _view_index(vue: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for entry in vue.get("files") or ():
        if isinstance(entry, Mapping) and isinstance(entry.get("file"), str):
            index[entry["file"]] = entry
    return index


def _labels(entry: Mapping[str, Any], bucket: str, field: str) -> list[Any]:
    labels = []
    for item in entry.get(bucket) or ():
        if isinstance(item, Mapping) and field in item:
            labels.append(item[field])
    return labels


def _elements(entry: Mapping[str, Any]) -> tuple[CardElement, ...]:
    collected: dict[tuple[str, str], CardElement] = {}
    for bucket, field, role in ELEMENT_BUCKETS:
        for label in _labels(entry, bucket, field):
            element = CardElement(role=role, label=label)
            collected.setdefault((role, label_text(label)), element)
    return tuple(collected[key] for key in sorted(collected))


def _actions(entry: Mapping[str, Any]) -> tuple[CardAction, ...]:
    collected: dict[tuple[str, str, str], CardAction] = {}
    for bucket, field, role in ELEMENT_BUCKETS:
        kind = {"button": "click", "dialog": "dialog", "tab": "tab"}.get(role)
        if kind is None:
            continue
        for label in _labels(entry, bucket, field):
            action = CardAction(kind=kind, target=label)
            collected.setdefault((kind, label_text(label), ""), action)
    for message in entry.get("messages") or ():
        if not isinstance(message, Mapping) or not isinstance(message.get("text"), str):
            continue
        level = message.get("type") if isinstance(message.get("type"), str) else ""
        action = CardAction(kind="message", target=message["text"], level=level)
        collected.setdefault(("message", message["text"], level), action)
    return tuple(collected[key] for key in sorted(collected))


def _landmark_label(node: NavNode, elements: Sequence[CardElement]) -> str:
    prefix = "element="
    for landmark in node.landmarks:
        if landmark.startswith(prefix) and landmark[len(prefix) :].strip():
            return landmark[len(prefix) :]
    for element in elements:
        if element.role == "button" and isinstance(element.label, str):
            return element.label
    return ""


def _assertions(node: NavNode, elements: Sequence[CardElement]) -> tuple[CardAssertion, ...]:
    assertions = [CardAssertion(kind="url", value=node.path)]
    if node.title:
        assertions.append(CardAssertion(kind="text", value=node.title))
    label = _landmark_label(node, elements)
    if label:
        assertions.append(CardAssertion(kind="element", value=label))
    return tuple(assertions)


def _confusables(labels: Sequence[str]) -> tuple[Confusable, ...]:
    unique = sorted({label.strip() for label in labels if isinstance(label, str) and len(label.strip()) >= 2})
    scored: list[tuple[float, str, str]] = []
    for left in range(len(unique)):
        for right in range(left + 1, len(unique)):
            ratio = difflib.SequenceMatcher(None, unique[left], unique[right]).ratio()
            if ratio >= CONFUSABLE_RATIO:
                scored.append((ratio, unique[left], unique[right]))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return tuple(
        Confusable(a=left, b=right, reason="shared_prefix" if left[:2] == right[:2] else "similar")
        for _, left, right in scored[:MAX_CONFUSABLES]
    )


def build_card(node: NavNode, entry: Mapping[str, Any] | None, structure_version: str) -> PageCard:
    view = entry or {}
    elements = _elements(view)
    actions = _actions(view)
    labels = [element.label for element in elements] + [action.target for action in actions]
    dynamic_labels = sum(1 for label in labels if is_dynamic(label))
    return PageCard(
        node=node.id,
        structure_version=structure_version,
        path=node.path,
        title=node.title,
        menu=node.menu,
        required_query=node.required_query,
        elements=elements,
        actions=actions,
        confusables=_confusables([label for label in labels if isinstance(label, str)]),
        assertions=_assertions(node, elements),
        dynamic_labels=dynamic_labels,
    )


def build_cards(graph: NavGraph, vue: Mapping[str, Any], structure_version: str) -> tuple[PageCard, ...]:
    """为导航图里的每个节点生成知识卡，按节点 id 排序。"""
    index = _view_index(vue)
    return tuple(build_card(node, index.get(node.component), structure_version) for node in graph.nodes)
