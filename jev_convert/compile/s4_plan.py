"""S4：把落地结果组装成执行计划（docs/implementation.md 第 4.1 节）。

入口由导航图节点生成：direct=yes，或 required_query 都能由 [test_data] 补齐时直接开 url
（参数写成 ${data.xxx} 占位，取值只在 Runner 执行时替换）；否则走菜单导航。
用例中途换页时（S3 给了 destination，且不等于本步所在页），在触发换页的**这一步**补上目的页的
到站地标断言，url= 也由 S5 按目的页的知识卡生成；下一步不再补。
用例 id 由 plan.make_case_id(入口页, source.node_id) 生成，与 index 无关。

build_plan 的前四个参数与接口契约一致；知识卡、测试数据、每步预算、是否生成 S5 断言
是关键字参数（都有默认值），流水线按配置传入，直接按契约调用也能用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from jev_convert.compile import plan, s5_assert
from jev_convert.compile.draft import GroundedCase, GroundedStep
from jev_convert.kb.cards import PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode

URL_ROUTE = "url"
MENU_ROUTE = "menu"
DEFAULT_STEP_BUDGET = 15  # 与 qa.toml 的 budget.step_actions_default 默认值一致
SEMANTIC_ONLY_CONFIDENCE = 0.5
SEMANTIC_PREFIX = "semantic="


class PlanBuildError(plan.PlanError):
    """组装计划所需的信息不全：页面节点不存在、没有步骤、预算放不下等。"""


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _node(graph: NavGraph, node_id: str) -> NavNode:
    for node in graph.nodes:
        if node.id == node_id:
            return node
    raise PlanBuildError(f"页面节点 {node_id!r} 不在导航图里：先用 kb build 生成导航图，或修正在地结果")


def _path(node: NavNode) -> str:
    path = node.path.strip()
    if not path:
        raise PlanBuildError(f"页面节点 {node.id} 没有 path，无法生成入口")
    return path


def _url_with_placeholders(node: NavNode) -> str:
    """把 required_query 写成 ${data.xxx} 占位；真实取值只在 Runner 执行时替换。"""
    path = _path(node)
    query = "&".join(f"{key}=${{data.{key}}}" for key in node.required_query)
    return f"{path}?{query}" if query else path


def entry_for(graph: NavGraph, node_id: str, test_data: Mapping[str, str]) -> plan.Entry:
    """由导航图节点生成入口：能直接开 url 就开 url，否则按菜单导航。"""
    node = _node(graph, node_id)
    missing = [key for key in node.required_query if key not in test_data]
    if node.direct == "yes" or (node.direct == "with_params" and not missing):
        return plan.Entry(node=node.id, route=URL_ROUTE, url=_url_with_placeholders(node), arrive=node.landmarks)
    # route=menu：url 只记录目标页 path，Runner 按 route 走菜单导航，不直接打开它。
    return plan.Entry(node=node.id, route=MENU_ROUTE, url=_path(node), arrive=node.landmarks)


def _step_budget(count: int, step_budget: int) -> int:
    """每步预算：只为兼容旧计划格式保留，取配置值；新执行层不再使用（docs/agent-design.md 12.4）。"""
    if step_budget < 1:
        raise PlanBuildError(f"每步预算至少为 1，实际是 {step_budget}")
    return step_budget


def _landmarks(node: NavNode | None) -> tuple[str, ...]:
    return node.landmarks if node is not None else ()


def _semantic_only(expects: Iterable[str]) -> bool:
    items = tuple(expects)
    return bool(items) and all(item.strip().startswith(SEMANTIC_PREFIX) for item in items)


def _changes_page(step: GroundedStep) -> bool:
    """这一步会不会换页：S3 给了 destination，且不是本步所在页。"""
    return bool(step.destination) and step.destination != step.page


def _expect_of(
    step: GroundedStep, card: PageCard | None, destination_card: PageCard | None, asserts: bool
) -> tuple[str, ...]:
    """导图里的原文预期排在前面，S5 生成的规则断言补在后；两者都必须是 dsl 表达式。

    原文预期写的是这一步执行后的状态：换页时按目的页的知识卡转换，否则按本页。
    """
    incoming = s5_assert.incoming_expects(step.expect, destination_card or card)
    generated = s5_assert.step_expect(step, card, destination_card) if asserts else ()
    return _unique((*incoming, *generated))


UNGROUNDED_CONFIDENCE = 0.35


def _fallback_name(step: GroundedStep) -> str:
    """没落地的步骤用导图原文兜底，保住 draft 计划的可读性；置信度保持低位。"""
    text = step.original.strip() or step.goal.strip() or step.source_node
    return text[:80]


def _target_of(step: GroundedStep, expect: tuple[str, ...]) -> plan.Target:
    confidence = SEMANTIC_ONLY_CONFIDENCE if _semantic_only(expect) else step.confidence
    name = step.target.name
    if not name:
        name = _fallback_name(step)
        confidence = min(confidence, UNGROUNDED_CONFIDENCE)
    return plan.Target(
        name=name,
        confidence=confidence,
        evidence=step.target.evidence,
        not_=step.target.not_,
        locator=step.target.locator,
        value=step.target.value,
    )


def build_plan(
    case: GroundedCase,
    graph: NavGraph,
    source: plan.SourceRef,
    index: int,
    *,
    cards: Mapping[str, PageCard] | None = None,
    test_data: Mapping[str, str] | None = None,
    step_budget: int = DEFAULT_STEP_BUDGET,
    asserts: bool = True,
) -> plan.Plan:
    """把一条落地用例变成执行计划；seq 从 1 连续，status 先统一为 draft，由流水线决定。

    index 只为保持接口契约而保留，不再参与 id：id 由 plan.make_case_id(入口页, source.node_id) 决定，
    导图里增删别的用例不会改到它。
    """
    del index  # 见上：不参与 id。
    if not case.steps:
        raise PlanBuildError(f"用例 {case.source_node} 没有步骤，不生成计划")
    pages = {node.id: node for node in graph.nodes}
    entry = entry_for(graph, case.entry_node or case.source_node, test_data or {})
    budget = _step_budget(len(case.steps), step_budget)

    steps: list[plan.Step] = []
    for position, step in enumerate(case.steps, start=1):
        card = (cards or {}).get(step.page)
        moves = _changes_page(step)
        destination_card = (cards or {}).get(step.destination) if moves else None
        expect = _expect_of(step, card, destination_card, asserts)
        if moves:
            # 到站地标属于触发换页的这一步：执行完它才应该看到目的页。
            expect = _unique((*expect, *_landmarks(pages.get(step.destination))))
        steps.append(
            plan.Step(
                seq=position,
                source_node=step.source_node,
                original=step.original,
                page=step.page,
                kind=step.kind,
                goal=step.goal,
                target=_target_of(step, expect),
                expect=expect,
                budget=budget,
                status="draft",
            )
        )

    title = case.title.strip() or case.source_node.strip()
    if not title:
        raise PlanBuildError("用例没有标题，不生成计划")
    return plan.Plan(
        id=plan.make_case_id(entry.node or case.source_node, source.node_id or case.source_node),
        title=title,
        status="draft",
        source=source,
        kb_version=case.kb_version.strip() or graph.kb_version,
        requires=_unique(case.requires),
        entry=entry,
        steps=tuple(steps),
        automatable=not case.not_automatable,
    )


def with_status(built: plan.Plan, status: str) -> plan.Plan:
    """用例与它的所有步骤用同一个状态（Runner 只执行全部 confirmed 的用例）。"""
    return replace(
        built,
        status=status,
        steps=tuple(replace(step, status=status) for step in built.steps),
        teardown=tuple(replace(step, status=status) for step in built.teardown),
    )
