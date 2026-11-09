"""写法画像（S1 前半）：从节点树推断写法，并按画像给节点标角色。

画像是数据：候选画像来自内置画像库、从节点树观察到的标记/关键词，以及 Llm.propose_profiles()
的抽样结果；每个候选都套用同一套标注逻辑（analyze），按结构约束算出 fit，choose_profile 取最高者。
新增一种写法只需要往画像库 LIBRARY_COMBOS 里加一行模板，标注逻辑里不加分支。

结构约束固定写在这里（见 fit 与 _line_role）：用例至少一个步骤；预期挂在步骤上；
前置在步骤之前；模块与功能点只在用例上层。

separators 只描述写法（同一节点里出现过的分隔符），供 S2 拆分复合步骤，不参与标注。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jev_convert.compile.s0_read import MindNode

if TYPE_CHECKING:
    from jev_convert.compile.llm import Llm

logger = logging.getLogger(__name__)

ROLES = ("module", "feature", "case", "step", "expect", "precondition", "unknown")
SCOPES = ("step", "case")
PROFILE_FIELDS = (
    "case_depth",
    "module_depths",
    "step_marker",
    "expect_marker",
    "precondition_keywords",
    "separators",
    "scope",
)

EXPECT_WORDS = ("预期结果", "预期效果", "期望结果", "预期", "期望", "校验点", "验收点")
STEP_WORDS = ("操作步骤", "测试步骤", "步骤")
INLINE_MARKERS = ("→", "->", "=>", "⇒", "—>", "➔")
PRECONDITION_WORDS = ("前置条件", "前置", "前提条件", "前提", "预置条件", "准备工作", "准备")
SEPARATOR_WORDS = ("→", "->", "=>", "⇒", "；", ";", "，", ",", "。", "|")

# 画像库：每行是一种常见写法（步骤标记, 预期标记, 前置关键词, 预期归属）。
LIBRARY_COMBOS = (
    ("", "预期", ("前置条件", "前置", "前提条件", "前提"), "step"),
    ("", "→", ("前置条件", "前置", "前提条件", "前提"), "step"),
    ("", "预期", (), "step"),
    ("", "→", (), "step"),
    ("步骤", "预期", ("前置条件", "前置"), "step"),
    ("", "预期", ("前置条件", "前置"), "case"),
)

WEIGHT_COVERAGE = 0.30
WEIGHT_CASES = 0.40
WEIGHT_SCOPE = 0.15
WEIGHT_ORDER = 0.15

PREFIX_LENGTH = 4
PREFIX_COUNT = 8
SAMPLE_COUNT = 5
SAMPLE_DEPTH = 6
FIT_DIGITS = 4

_TRIM = "：:，,、;；-—> \t"
_SPACE = re.compile(r"\s+")
_LEAD = re.compile(r"^(?:\(?\d+[.、)）]|[①-⑳]|[（(][一二三四五六七八九十\d]+[)）]|[-*•·])\s*")


class ProfileError(ValueError):
    """写法画像不合法：字段缺失、类型不对，或 case_depth 为负数。"""


def _clean(text: str) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def bare(text: str) -> str:
    """去掉行首编号（1. / 1、/ ①/ - / •），用于匹配标记与前缀。"""
    return _LEAD.sub("", _clean(text))


def expect_prefix(text: str, marker: str) -> str | None:
    """前缀写法（预期：x）里的预期内容：节点本身就是预期，取不到返回 None。"""
    if not marker:
        return None
    head = bare(text)
    if not head.lower().startswith(marker.lower()):
        return None
    return head[len(marker) :].lstrip(_TRIM) or None


def expect_text(text: str, marker: str) -> str | None:
    """按标记取预期内容：前缀写法（预期：x）和行内写法（动作 → x）都返回 x，取不到返回 None。"""
    prefixed = expect_prefix(text, marker)
    if prefixed:
        return prefixed
    if not marker:
        return None
    clean = _clean(text)
    index = clean.find(marker)
    if index <= 0 or not clean[:index].strip():
        return None
    return clean[index + len(marker) :].lstrip(_TRIM) or None


def _has_keyword(text: str, keywords: Sequence[str]) -> bool:
    head = bare(text).lower()
    return any(keyword and head.startswith(keyword.lower()) for keyword in keywords)


def _has_precondition(profile: Profile, text: str) -> bool:
    return _has_keyword(text, profile.precondition_keywords)


def _step_matches(profile: Profile, text: str) -> bool:
    """step_marker 为空表示「无步骤标记」：用例下的子节点就是步骤。"""
    if not profile.step_marker:
        return True
    return bare(text).lower().startswith(profile.step_marker.lower())


def _scope(profile: Profile) -> str:
    return "case" if profile.scope == "case" else "step"


def _line_role(profile: Profile, text: str, *, in_step: bool) -> str:
    """按画像判断一行文字的角色；判不出返回 unknown，交给调用方（LLM 或人工）。

    只有前缀写法（预期：x）才表示「这个节点是预期」；行内写法（动作 → x）仍是步骤，
    预期在 _step_shape 里从同一节点拆出来。
    """
    if _has_precondition(profile, text):
        return "precondition"
    if expect_prefix(text, profile.expect_marker) is not None:
        return "expect"
    if in_step:
        return "unknown"
    return "step" if _step_matches(profile, text) else "unknown"


def _resolved(profile: Profile, text: str, *, in_step: bool, override: str | None) -> str:
    role = _line_role(profile, text, in_step=in_step)
    if role == "unknown" and override in ROLES and override != "unknown":
        return str(override)
    return role


def _index(
    nodes: Sequence[MindNode],
) -> tuple[dict[str, MindNode], dict[str | None, list[MindNode]], dict[str, int], tuple[str, ...]]:
    """建索引：节点表、父子表（兄弟按 order）、深度、深度优先的节点 id 顺序。"""
    items = list(nodes)
    by_id: dict[str, MindNode] = {}
    for node in items:
        by_id.setdefault(node.id, node)
    position = {node.id: index for index, node in enumerate(items)}
    children: dict[str | None, list[MindNode]] = {}
    for node in items:
        parent = node.parent if node.parent in by_id and node.parent != node.id else None
        children.setdefault(parent, []).append(node)
    for siblings in children.values():
        siblings.sort(key=lambda item: (item.order, position.get(item.id, 0)))

    depths: dict[str, int] = {}
    order: list[str] = []
    stack = [(node, 0) for node in reversed(children.get(None, ()))]
    while stack:
        node, depth = stack.pop()
        if node.id in depths:
            continue
        depths[node.id] = depth
        order.append(node.id)
        for child in reversed(children.get(node.id, ())):
            stack.append((child, depth + 1))
    for node in items:  # 父链成环等异常：当作根，保证每个节点都有深度与顺序
        if node.id not in depths:
            depths[node.id] = 0
            order.append(node.id)
    return by_id, children, depths, tuple(order)


@dataclass(frozen=True)
class Profile:
    case_depth: int
    module_depths: tuple[int, ...]
    step_marker: str
    expect_marker: str
    precondition_keywords: tuple[str, ...]
    separators: tuple[str, ...]
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_depth": self.case_depth,
            "module_depths": list(self.module_depths),
            "step_marker": self.step_marker,
            "expect_marker": self.expect_marker,
            "precondition_keywords": list(self.precondition_keywords),
            "separators": list(self.separators),
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Profile:
        where = "profile"
        body = _check_mapping(data, where)
        _check_fields(body, where, PROFILE_FIELDS)
        case_depth = _check_int(body, where, "case_depth")
        if case_depth < 0:
            raise ProfileError(f"{where}.case_depth 不能为负数，实际是 {case_depth}")
        depths = _check_ints(body, where, "module_depths")
        if any(depth < 0 for depth in depths):
            raise ProfileError(f"{where}.module_depths 不能有负数：{depths}")
        return cls(
            case_depth=case_depth,
            module_depths=depths,
            step_marker=_check_text(body, where, "step_marker"),
            expect_marker=_check_text(body, where, "expect_marker"),
            precondition_keywords=_check_texts(body, where, "precondition_keywords"),
            separators=_check_texts(body, where, "separators"),
            scope=_check_text(body, where, "scope"),
        )

    @property
    def key(self) -> tuple[Any, ...]:
        return (
            self.case_depth,
            self.module_depths,
            self.step_marker,
            self.expect_marker,
            self.precondition_keywords,
            self.separators,
            self.scope,
        )


def _check_mapping(data: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ProfileError(f"{where} 必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


def _check_fields(data: Mapping[str, Any], where: str, required: Sequence[str]) -> None:
    unknown = sorted(set(data) - set(required))
    if unknown:
        raise ProfileError(f"{where} 含未知字段：{', '.join(unknown)}")
    missing = [name for name in required if name not in data]
    if missing:
        raise ProfileError(f"{where} 缺少必填字段：{', '.join(missing)}")


def _check_text(data: Mapping[str, Any], where: str, key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ProfileError(f"{where}.{key} 必须是字符串，实际是 {type(value).__name__}")
    return value


def _check_texts(data: Mapping[str, Any], where: str, key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{where}.{key} 必须是字符串列表")
    return tuple(value)


def _check_int(data: Mapping[str, Any], where: str, key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError(f"{where}.{key} 必须是整数，实际是 {type(value).__name__}")
    return int(value)


def _check_ints(data: Mapping[str, Any], where: str, key: str) -> tuple[int, ...]:
    value = data[key]
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}.{key} 必须是整数列表")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ProfileError(f"{where}.{key} 必须是整数列表")
        result.append(int(item))
    return tuple(result)


@dataclass(frozen=True)
class StepDraft:
    source_node: str
    original: str
    expect: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"source_node": self.source_node, "original": self.original, "expect": list(self.expect)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepDraft:
        where = "step"
        body = _check_mapping(data, where)
        _check_fields(body, where, ("source_node", "original", "expect"))
        return cls(
            source_node=_check_text(body, where, "source_node"),
            original=_check_text(body, where, "original"),
            expect=_check_texts(body, where, "expect"),
        )


@dataclass(frozen=True)
class CaseDraft:
    source_node: str
    title: str
    menu: tuple[str, ...]
    preconditions: tuple[str, ...]
    steps: tuple[StepDraft, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_node": self.source_node,
            "title": self.title,
            "menu": list(self.menu),
            "preconditions": list(self.preconditions),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaseDraft:
        where = "case"
        body = _check_mapping(data, where)
        _check_fields(body, where, ("source_node", "title", "menu", "preconditions", "steps"))
        steps = body["steps"]
        if not isinstance(steps, (list, tuple)):
            raise ProfileError(f"{where}.steps 必须是数组，实际是 {type(steps).__name__}")
        return cls(
            source_node=_check_text(body, where, "source_node"),
            title=_check_text(body, where, "title"),
            menu=_check_texts(body, where, "menu"),
            preconditions=_check_texts(body, where, "preconditions"),
            steps=tuple(StepDraft.from_dict(item) for item in steps),
        )


@dataclass(frozen=True)
class StepShape:
    node: MindNode
    expects: tuple[str, ...]
    inline: bool


@dataclass(frozen=True)
class CaseShape:
    node: MindNode
    menu: tuple[str, ...]
    preconditions: tuple[MindNode, ...]
    steps: tuple[StepShape, ...]


@dataclass(frozen=True)
class Analysis:
    roles: Mapping[str, str]
    cases: tuple[CaseShape, ...]
    origins: Mapping[str, int]
    unresolved: tuple[str, ...]
    known: int
    total: int


def _menu(by_id: Mapping[str, MindNode], node: MindNode) -> tuple[str, ...]:
    chain: list[str] = []
    seen = {node.id}
    current = node
    while current.parent in by_id and current.parent not in seen:
        current = by_id[current.parent]
        seen.add(current.id)
        chain.append(_clean(current.text))
    return tuple(reversed(chain))


def _ancestor_preconditions(
    by_id: Mapping[str, MindNode],
    children: Mapping[str | None, Sequence[MindNode]],
    node: MindNode,
    profile: Profile,
) -> tuple[MindNode, ...]:
    """用例上层、排在所在分支之前的兄弟节点里写的前置：由外到内。"""
    chain: list[MindNode] = []
    seen = {node.id}
    current = node
    while current.parent in by_id and current.parent not in seen:
        current = by_id[current.parent]
        seen.add(current.id)
        chain.append(current)
    found: list[MindNode] = []
    for ancestor in reversed(chain):
        for sibling in children.get(ancestor.parent, ()):
            if sibling.id == ancestor.id:
                break
            if _has_precondition(profile, sibling.text):
                found.append(sibling)
    return tuple(found)


def _step_shape(
    profile: Profile,
    node: MindNode,
    children: Mapping[str | None, Sequence[MindNode]],
    roles: dict[str, str],
    origins: dict[str, int],
    overrides: Mapping[str, str],
) -> StepShape:
    expects: list[str] = []
    inline = False
    text = expect_text(node.text, profile.expect_marker)
    if text:
        expects.append(text)
        inline = True
        origins["inline"] = origins.get("inline", 0) + 1
    for child in children.get(node.id, ()):
        role = _resolved(profile, child.text, in_step=True, override=overrides.get(child.id))
        if role != "expect":
            roles.setdefault(child.id, "unknown")
            continue
        roles[child.id] = "expect"
        expects.append(expect_text(child.text, profile.expect_marker) or _clean(child.text))
        origins["child"] = origins.get("child", 0) + 1
    return StepShape(node=node, expects=tuple(expects), inline=inline)


def analyze(profile: Profile, nodes: Sequence[MindNode], overrides: Mapping[str, str] | None = None) -> Analysis:
    """按画像整棵树标角色，并组合出用例结构。overrides 只用于画像判不出（unknown）的节点。"""
    items = tuple(nodes)
    by_id, children, depths, order = _index(items)
    decided = dict(overrides or {})
    case_depth = max(profile.case_depth, 0)
    roles: dict[str, str] = {}
    for node in items:
        depth = depths.get(node.id, 0)
        if depth < case_depth:
            roles[node.id] = (
                "precondition"
                if _has_precondition(profile, node.text)
                else ("module" if depth in profile.module_depths else "feature")
            )
        elif depth == case_depth:
            roles[node.id] = "precondition" if _has_precondition(profile, node.text) else "case"
        else:
            roles[node.id] = "unknown"

    cases: list[CaseShape] = []
    origins: dict[str, int] = {}
    for node_id in order:
        node = by_id.get(node_id)
        if node is None or roles.get(node_id) != "case":
            continue
        preconditions = list(_ancestor_preconditions(by_id, children, node, profile))
        steps: list[StepShape] = []
        pending: list[str] = []
        for child in children.get(node_id, ()):
            role = _resolved(profile, child.text, in_step=False, override=decided.get(child.id))
            if role == "precondition":
                roles[child.id] = "precondition"
                preconditions.append(child)
                continue
            if role == "expect":
                roles[child.id] = "expect"
                text = expect_text(child.text, profile.expect_marker) or _clean(child.text)
                if steps:
                    steps[-1] = StepShape(
                        node=steps[-1].node,
                        expects=steps[-1].expects + (text,),
                        inline=steps[-1].inline,
                    )
                    origins["sibling"] = origins.get("sibling", 0) + 1
                else:
                    pending.append(text)
                continue
            if role != "step":
                roles[child.id] = "unknown"
                continue
            roles[child.id] = "step"
            steps.append(_step_shape(profile, child, children, roles, origins, decided))
        if pending and steps:
            steps[0] = StepShape(
                node=steps[0].node,
                expects=tuple(pending) + steps[0].expects,
                inline=steps[0].inline,
            )
            origins["sibling"] = origins.get("sibling", 0) + len(pending)
        cases.append(
            CaseShape(
                node=node,
                menu=_menu(by_id, node),
                preconditions=tuple(preconditions),
                steps=tuple(steps),
            )
        )

    unresolved = tuple(node_id for node_id in order if roles.get(node_id) == "unknown")
    known = sum(1 for role in roles.values() if role != "unknown")
    return Analysis(
        roles=roles,
        cases=tuple(cases),
        origins=origins,
        unresolved=unresolved,
        known=known,
        total=len(items),
    )


def _cases_score(analysis: Analysis) -> float:
    """用例至少一个步骤；每个步骤都要有预期。"""
    if not analysis.cases:
        return 0.0
    scores = [
        1.0 if case.steps and all(step.expects for step in case.steps) else 0.0 for case in analysis.cases
    ]
    return sum(scores) / len(scores)


def _scope_score(profile: Profile, analysis: Analysis) -> float:
    """预期挂在步骤上（行内或子节点），还是挂在用例层（步骤的兄弟节点）。"""
    total = sum(analysis.origins.values())
    if not total:
        return 0.0
    wanted = ("sibling",) if _scope(profile) == "case" else ("inline", "child")
    return sum(analysis.origins.get(kind, 0) for kind in wanted) / total


def _order_score(analysis: Analysis) -> float:
    """前置在步骤之前。"""
    if not analysis.cases:
        return 0.0
    scores: list[float] = []
    for case in analysis.cases:
        first_step = case.steps[0].node.order if case.steps else None
        if first_step is None:
            scores.append(0.0 if case.preconditions else 1.0)
            continue
        late = [pre for pre in case.preconditions if pre.order > first_step]
        scores.append(1.0 if not late else 0.0)
    return sum(scores) / len(scores)


def fit(profile: Profile, nodes: Sequence[MindNode], overrides: Mapping[str, str] | None = None) -> float:
    """画像与整棵树的吻合度，取值 [0, 1]；越高越吻合，低于阈值由调用方决定不生成计划。

    overrides：人工在确认表里标注的节点角色（画像判不出的节点按标注算）。
    """
    items = tuple(nodes)
    if not items:
        return 0.0
    analysis = analyze(profile, items, overrides)
    coverage = analysis.known / analysis.total if analysis.total else 0.0
    score = (
        WEIGHT_COVERAGE * coverage
        + WEIGHT_CASES * _cases_score(analysis)
        + WEIGHT_SCOPE * _scope_score(profile, analysis)
        + WEIGHT_ORDER * _order_score(analysis)
    )
    return round(min(max(score, 0.0), 1.0), FIT_DIGITS)


def _observed_markers(items: Sequence[MindNode], vocabulary: Sequence[str]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for node in items:
        clean = _clean(node.text)
        for marker in vocabulary:
            if marker and marker in clean:
                counts[marker] = counts.get(marker, 0) + 1
    return tuple(sorted(counts, key=lambda marker: (-counts[marker], -len(marker), marker)))


def _case_depths(
    items: Sequence[MindNode],
    children: Mapping[str | None, Sequence[MindNode]],
    depths: Mapping[str, int],
    expect_markers: Sequence[str],
) -> tuple[int, ...]:
    """可能是用例层的深度：子节点还有子节点（用例→步骤→预期），或子节点带行内预期标记。"""
    found: set[int] = set()
    for node in items:
        kids = children.get(node.id, ())
        if any(children.get(child.id) for child in kids):
            found.add(depths.get(node.id, 0))
        if any(expect_text(child.text, marker) for child in kids for marker in expect_markers):
            found.add(depths.get(node.id, 0))
    if not found:
        found.add(max(max(depths.values(), default=1) - 1, 0))
    return tuple(sorted(found))


def _prefixes(items: Sequence[MindNode]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for node in items:
        head = bare(node.text)[:PREFIX_LENGTH]
        if head:
            counts[head] = counts.get(head, 0) + 1
    ordered = sorted(counts, key=lambda head: (-counts[head], head))[:PREFIX_COUNT]
    return tuple((head, counts[head]) for head in ordered)


def extract_features(nodes: Sequence[MindNode]) -> dict[str, Any]:
    """提取画像推断要用的特征：层级分布、叶子形态、常见标记、关键词、分隔符。"""
    items = tuple(nodes)
    _by_id, children, depths, _order = _index(items)
    counts: dict[int, int] = {}
    for node in items:
        depth = depths.get(node.id, 0)
        counts[depth] = counts.get(depth, 0) + 1
    branching: dict[int, float] = {}
    for depth in sorted(counts):
        level = [node for node in items if depths.get(node.id, 0) == depth]
        kids = sum(len(children.get(node.id, ())) for node in level)
        branching[depth] = round(kids / len(level), 3) if level else 0.0
    expect_markers = _observed_markers(items, (*EXPECT_WORDS, *INLINE_MARKERS))
    return {
        "count": len(items),
        "depth_counts": {depth: counts[depth] for depth in sorted(counts)},
        "max_depth": max(counts) if counts else 0,
        "roots": len(children.get(None, ())),
        "leaves": sum(1 for node in items if not children.get(node.id)),
        "branching": branching,
        "case_depths": _case_depths(items, children, depths, expect_markers),
        "expect_markers": expect_markers,
        "step_markers": _observed_markers(items, STEP_WORDS),
        "precondition_hits": tuple(
            word for word in PRECONDITION_WORDS if any(_has_keyword(node.text, (word,)) for node in items)
        ),
        "separators": tuple(
            word for word in SEPARATOR_WORDS if any(word in _clean(node.text) for node in items)
        ),
        "prefixes": _prefixes(items),
        "numbered": any(_LEAD.match(_clean(node.text)) for node in items),
    }


def _module_variants(case_depth: int) -> tuple[tuple[int, ...], ...]:
    if case_depth <= 0:
        return ((),)
    variants = [tuple(range(case_depth))]
    if case_depth > 1:
        variants.append((0,))
    return tuple(variants)


def _library(features: Mapping[str, Any]) -> list[Profile]:
    separators = tuple(features["separators"])
    found: list[Profile] = []
    for case_depth in features["case_depths"]:
        for step_marker, expect_marker, keywords, scope in LIBRARY_COMBOS:
            for module_depths in _module_variants(case_depth):
                found.append(
                    Profile(
                        case_depth=case_depth,
                        module_depths=module_depths,
                        step_marker=step_marker,
                        expect_marker=expect_marker,
                        precondition_keywords=tuple(keywords),
                        separators=separators,
                        scope=scope,
                    )
                )
    return found


def _observed(features: Mapping[str, Any]) -> list[Profile]:
    separators = tuple(features["separators"])
    expect_markers = (*features["expect_markers"], "")
    step_markers = (*features["step_markers"], "")
    hits = tuple(features["precondition_hits"])
    keyword_sets = (hits, ()) if hits else ((),)
    found: list[Profile] = []
    for case_depth in features["case_depths"]:
        for expect_marker in expect_markers:
            for step_marker in step_markers:
                for keywords in keyword_sets:
                    for scope in SCOPES:
                        for module_depths in _module_variants(case_depth):
                            found.append(
                                Profile(
                                    case_depth=case_depth,
                                    module_depths=module_depths,
                                    step_marker=step_marker,
                                    expect_marker=expect_marker,
                                    precondition_keywords=keywords,
                                    separators=separators,
                                    scope=scope,
                                )
                            )
    return found


def _samples(items: Sequence[MindNode]) -> list[str]:
    """抽样出几棵子树给 propose_profiles()：根节点的子树，按顺序取前 SAMPLE_COUNT 棵。"""
    _by_id, children, _depths, _order = _index(items)
    roots = children.get(None, ())
    if not roots:
        return ["\n".join(node.text for node in items)]

    def render(node: MindNode, level: int) -> list[str]:
        head = f"{'  ' * level}{node.text}"
        if level >= SAMPLE_DEPTH:
            return [head]
        lines = [head]
        for child in children.get(node.id, ()):
            lines.extend(render(child, level + 1))
        return lines

    return ["\n".join(render(node, 0)) for node in list(roots)[:SAMPLE_COUNT]]


def _from_llm(llm: Llm, items: Sequence[MindNode]) -> list[Profile]:
    try:
        proposed = llm.propose_profiles(_samples(items))
    except Exception as error:  # LLM 只是候选来源，失败时保留画像库的候选
        logger.warning("propose_profiles 失败，只用画像库候选：%s", error)
        return []
    found: list[Profile] = []
    for item in proposed:
        try:
            found.append(Profile.from_dict(item))
        except (ProfileError, ValueError) as error:
            logger.debug("丢弃不合法的画像：%s", error)
    return found


def _unique(profiles: Sequence[Profile]) -> list[Profile]:
    seen: dict[tuple[Any, ...], Profile] = {}
    for profile in profiles:
        seen.setdefault(profile.key, profile)
    return [seen[key] for key in sorted(seen)]


def _fallback_profile(nodes: Sequence[MindNode]) -> Profile:
    _by_id, children, depths, _order = _index(nodes)
    with_children = [depths.get(node.id, 0) for node in nodes if children.get(node.id)]
    case_depth = max(with_children) if with_children else 0
    return Profile(
        case_depth=case_depth,
        module_depths=tuple(range(case_depth)),
        step_marker="",
        expect_marker="预期",
        precondition_keywords=("前置条件", "前置"),
        separators=(),
        scope="step",
    )


def propose_candidates(nodes: Sequence[MindNode], llm: Llm | None = None) -> list[Profile]:
    """候选画像：画像库 + 从节点树观察到的标记组合 + propose_profiles()（可选），按 key 排序去重。"""
    items = tuple(nodes)
    features = extract_features(items)
    found = [*_library(features), *_observed(features)]
    if llm is not None:
        found.extend(_from_llm(llm, items))
    return _unique(found)


def choose_profile(nodes: Sequence[MindNode], llm: Llm | None = None,
                   overrides: Mapping[str, str] | None = None) -> tuple[Profile, float]:
    """套用每个候选画像算 fit，取最高者；同一输入永远得到同一个结果。"""
    items = tuple(nodes)
    candidates = propose_candidates(items, llm)
    if not candidates:
        return _fallback_profile(items), 0.0
    scored = [(fit(profile, items, overrides), profile) for profile in candidates]
    best_score, best = min(scored, key=lambda item: (-item[0], item[1].key))
    return best, best_score
