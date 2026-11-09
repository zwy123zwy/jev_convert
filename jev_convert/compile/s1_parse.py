"""S1 后半：按画像给节点标角色，组合成与写法无关的用例草稿。

角色来自画像（profile.analyze）；画像判不出的节点（unknown）才用 Llm.choose() 在 ROLES 里选，
没有 llm 时保持 unknown，不猜。用例草稿只保留有步骤的用例（结构约束：用例至少一个步骤）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from jev_convert.compile.profile import (
    ROLES,
    Analysis,
    CaseDraft,
    CaseShape,
    Profile,
    StepDraft,
    analyze,
    bare,
)
from jev_convert.compile.s0_read import MindNode, Mindnote

if TYPE_CHECKING:
    from jev_convert.compile.llm import Llm

ROLE_TASK = "给思维导图节点选角色"


def _context(node: MindNode, profile: Profile, roles: Mapping[str, str], by_id: Mapping[str, MindNode]) -> dict:
    parent = by_id.get(node.parent) if node.parent else None
    return {
        "node": node.id,
        "text": node.text,
        "order": node.order,
        "parent_text": parent.text if parent is not None else "",
        "parent_role": roles.get(parent.id, "") if parent is not None else "",
        "case_depth": profile.case_depth,
        "expect_marker": profile.expect_marker,
        "step_marker": profile.step_marker,
        "precondition_keywords": list(profile.precondition_keywords),
        "roles": list(ROLES),
    }


def _ask(llm: Llm | None, mindnote: Mindnote, profile: Profile, analysis: Analysis) -> dict[str, str]:
    """只对画像判不出的节点问 LLM：候选是固定的角色名表，答案必须落在表里。"""
    if llm is None or not analysis.unresolved:
        return {}
    by_id = {node.id: node for node in mindnote.nodes}
    overrides: dict[str, str] = {}
    for node_id in analysis.unresolved:
        node = by_id.get(node_id)
        if node is None:
            continue
        choice = llm.choose(ROLE_TASK, _context(node, profile, analysis.roles, by_id), ROLES)
        if choice.index is None or not 0 <= choice.index < len(ROLES):
            continue
        role = ROLES[choice.index]
        if role != "unknown":
            overrides[node_id] = role
    return overrides


def _analysis(mindnote: Mindnote, profile: Profile, llm: Llm | None,
              labels: Mapping[str, str] | None = None) -> Analysis:
    """人工标注（labels）优先，其余判不出的再问 LLM。"""
    known = dict(labels or {})
    analysis = analyze(profile, mindnote.nodes, known)
    overrides = _ask(llm, mindnote, profile, analysis)
    if not overrides:
        return analysis
    return analyze(profile, mindnote.nodes, {**overrides, **known})


def classify(mindnote: Mindnote, profile: Profile, llm: Llm | None = None) -> dict[str, str]:
    """节点 id → 角色（ROLES 之一）；LLM 修正过的节点也在里面。"""
    return dict(_analysis(mindnote, profile, llm).roles)


def _condition(text: str) -> str:
    """前置条件去掉关键词与分隔符，只留条件本身。"""
    head = bare(text)
    for keyword in ("前置条件", "前置", "前提条件", "前提", "预置条件", "准备工作", "准备"):
        if head.lower().startswith(keyword.lower()):
            return head[len(keyword) :].lstrip("：:，,、;； \t") or head
    return head


def _draft(case: CaseShape) -> CaseDraft:
    return CaseDraft(
        source_node=case.node.id,
        title=bare(case.node.text),
        menu=case.menu,
        preconditions=tuple(_condition(node.text) for node in case.preconditions),
        steps=tuple(
            StepDraft(source_node=step.node.id, original=" ".join(step.node.text.split()), expect=step.expects)
            for step in case.steps
        ),
    )


def parse(mindnote: Mindnote, profile: Profile, llm: Llm | None = None,
          labels: Mapping[str, str] | None = None) -> list[CaseDraft]:
    """按画像识别用例结构；顺序按导图里的 order，source_node 都是真实节点 id。"""
    return [_draft(case) for case in _analysis(mindnote, profile, llm, labels).cases if case.steps]


def unresolved(mindnote: Mindnote, profile: Profile, llm: Llm | None = None) -> tuple[str, ...]:
    """仍然判不出角色的节点 id（深度优先序）：调用方按「待确认」处理，不要猜。"""
    return _analysis(mindnote, profile, llm).unresolved
