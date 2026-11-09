from __future__ import annotations

from dataclasses import replace

import pytest

from jev_convert.compile import plan, s4_plan
from jev_convert.compile.draft import GroundedCase, GroundedStep
from jev_convert.compile.plan import SourceRef, Target
from jev_convert.kb.cards import CardAssertion, CardElement, PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode

KB_VERSION = "webui@test123"
SOURCE = SourceRef(mindnote="mn1", node_id="n_10", version="1792028614")
TEST_DATA = {"graph_definition_id": "4", "node_id": "supplier_sampling"}


def make_graph(direct: str = "with_params") -> NavGraph:
    nodes = (
        NavNode(id="workbench", path="/workbench", title="工作台", menu=("数据平台-新版", "工作台"),
                direct="yes", landmarks=("element=开始处理",)),
        NavNode(
            id="annotation_task_pool",
            path="/workbench/annotation/task-pool",
            title="任务池",
            menu=("数据平台-新版", "工作台", "标注工作台"),
            required_query=("graph_definition_id", "node_id"),
            direct=direct,
            landmarks=("element=开始处理",),
        ),
        NavNode(id="annotation_task", path="/workbench/annotation/task", title="任务详情",
                direct="yes", landmarks=("element=关闭工作台",)),
    )
    return NavGraph(kb_version=KB_VERSION, nodes=nodes, edges=())


def make_card(node: str, path: str, labels: tuple[str, ...]) -> PageCard:
    return PageCard(
        node=node,
        structure_version=KB_VERSION,
        path=path,
        title=node,
        elements=tuple(CardElement(role="button", label=label) for label in labels),
        assertions=(
            CardAssertion(kind="url", value=path),
            CardAssertion(kind="text", value=node),
            *(CardAssertion(kind="element", value=label) for label in labels[:1]),
        ),
    )


def make_cards() -> dict[str, PageCard]:
    return {
        "annotation_task_pool": make_card(
            "annotation_task_pool", "/workbench/annotation/task-pool", ("开始处理",)
        ),
        "annotation_task": make_card("annotation_task", "/workbench/annotation/task", ("提交", "关闭工作台")),
        "workbench": make_card("workbench", "/workbench", ("开始处理",)),
    }


def step(
    index: int,
    page: str,
    name: str,
    *,
    original: str = "点击按钮",
    kind: str = "click",
    confidence: float = 0.95,
    expect: tuple[str, ...] = (),
    destination: str = "",
) -> GroundedStep:
    return GroundedStep(
        seq=index,
        source_node=f"n_{index}",
        original=original,
        page=page,
        kind=kind,
        goal=f"点击“{name}”" if kind == "click" else original,
        target=Target(name=name, confidence=confidence, evidence=("source",)),
        confidence=confidence,
        evidence=("source",),
        expect=expect,
        destination=destination,
    )


def make_case(
    steps: tuple[GroundedStep, ...],
    *,
    entry_node: str = "annotation_task_pool",
    requires: tuple[str, ...] = ("logged_in", "has_pending_task", "logged_in"),
    not_automatable: bool = False,
) -> GroundedCase:
    return GroundedCase(
        source_node="n_10",
        title="处理一条任务",
        menu=("数据平台-新版", "工作台", "标注工作台"),
        requires=requires,
        entry_node=entry_node,
        kb_version=KB_VERSION,
        steps=steps,
        not_automatable=not_automatable,
    )


def test_entry_opens_url_when_page_is_direct() -> None:
    entry = s4_plan.entry_for(make_graph(), "workbench", {})

    assert entry.node == "workbench"
    assert entry.route == "url"
    assert entry.url == "/workbench"
    assert entry.arrive == ("element=开始处理",)


def test_entry_keeps_data_placeholders_for_required_query() -> None:
    entry = s4_plan.entry_for(make_graph(), "annotation_task_pool", TEST_DATA)

    assert entry.route == "url"
    assert entry.url == (
        "/workbench/annotation/task-pool"
        "?graph_definition_id=${data.graph_definition_id}&node_id=${data.node_id}"
    )
    assert TEST_DATA["graph_definition_id"] not in entry.url
    assert TEST_DATA["node_id"] not in entry.url


def test_entry_falls_back_to_menu_when_param_is_missing() -> None:
    entry = s4_plan.entry_for(make_graph(), "annotation_task_pool", {"graph_definition_id": "4"})

    assert entry.route == "menu"
    assert entry.url == "/workbench/annotation/task-pool"
    assert entry.arrive == ("element=开始处理",)


def test_entry_uses_menu_for_guarded_pages() -> None:
    entry = s4_plan.entry_for(make_graph(direct="menu"), "annotation_task_pool", TEST_DATA)

    assert entry.route == "menu"


def test_entry_for_unknown_node_raises() -> None:
    with pytest.raises(plan.PlanError):
        s4_plan.entry_for(make_graph(), "no_such_page", {})


def test_build_plan_has_continuous_seq_and_budget() -> None:
    case = make_case(
        (
            step(1, "annotation_task_pool", "开始处理"),
            step(2, "annotation_task", "提交"),
        )
    )

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert [item.seq for item in built.steps] == [1, 2]
    assert {item.budget for item in built.steps} == {s4_plan.DEFAULT_STEP_BUDGET}
    assert built.id == plan.make_case_id("annotation_task_pool", SOURCE.node_id)
    assert built.status == "draft"
    assert {item.status for item in built.steps} == {"draft"}
    assert built.requires == ("logged_in", "has_pending_task")
    assert built.automatable is True
    plan.validate(built)


def test_build_plan_keeps_step_budget_when_many_steps() -> None:
    """不再按 55 步总预算压缩每步预算（agent-design 12.4）。"""
    case = make_case(tuple(step(index, "annotation_task", "提交") for index in range(1, 13)))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert {item.budget for item in built.steps} == {s4_plan.DEFAULT_STEP_BUDGET}
    plan.validate(built)


def test_build_plan_adds_landmarks_to_the_step_that_changes_page() -> None:
    case = make_case(
        (
            step(1, "workbench", "开始处理", original="点击“开始处理”", expect=("进入任务详情",),
                 destination="annotation_task"),
            step(2, "annotation_task", "提交", original="点击“提交”"),
        ),
        entry_node="workbench",
    )

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert "element=关闭工作台" in built.steps[0].expect
    assert "url=/workbench/annotation/task" in built.steps[0].expect
    assert "url=/workbench" not in built.steps[0].expect
    assert "element=关闭工作台" not in built.steps[1].expect


def test_build_plan_without_destination_has_no_url_or_landmarks() -> None:
    case = make_case(
        (
            step(1, "workbench", "开始处理", original="点击“开始处理”", expect=("进入处理页面",)),
            step(2, "annotation_task", "提交", original="点击“提交”"),
        ),
        entry_node="workbench",
    )

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    for item in built.steps:
        assert not any(expr.startswith("url=") for expr in item.expect)
    assert "element=关闭工作台" not in built.steps[1].expect


def test_build_plan_id_does_not_depend_on_index() -> None:
    case = make_case((step(1, "annotation_task", "提交"),))

    first = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)
    second = s4_plan.build_plan(case, make_graph(), SOURCE, 7, cards=make_cards(), test_data=TEST_DATA)

    assert first.id == second.id


def test_build_plan_keeps_mindnote_expect_first() -> None:
    case = make_case((step(1, "annotation_task", "提交", expect=("text=任务详情",)),))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert built.steps[0].expect[0] == "text=任务详情"


def test_build_plan_gives_every_step_a_rule_assertion() -> None:
    case = make_case(
        (
            step(1, "annotation_task_pool", "开始处理", original="进入任务池，点击“开始处理”",
                 expect=("进入任务详情",), destination="annotation_task"),
            step(2, "annotation_task", "提交", expect=("出现关闭工作台",)),
        )
    )

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    for item in built.steps:
        assert item.expect
        assert any(not expr.startswith("semantic=") for expr in item.expect)


def test_build_plan_lowers_confidence_for_semantic_only_steps() -> None:
    case = make_case(
        (step(1, "annotation_task", "任务状态变为已完成", original="检查任务状态", confidence=0.95),),
        entry_node="annotation_task",
    )

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert built.steps[0].expect == ("semantic=检查任务状态",)
    assert built.steps[0].target.confidence == 0.5


def test_build_plan_keeps_confidence_for_rule_steps() -> None:
    case = make_case((step(1, "annotation_task", "提交", confidence=0.95, expect=("出现关闭工作台",)),))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert built.steps[0].expect == ("semantic=出现关闭工作台", "element=关闭工作台")
    assert built.steps[0].target.confidence == pytest.approx(0.95)


def test_build_plan_caps_self_fulfilling_click_at_semantic_confidence() -> None:
    case = make_case((step(1, "annotation_task", "提交", confidence=0.95),))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert "element=提交" not in built.steps[0].expect
    assert built.steps[0].target.confidence == 0.5


def test_build_plan_marks_case_not_automatable() -> None:
    case = make_case((step(1, "annotation_task", "提交"),), not_automatable=True)

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert built.automatable is False


def test_build_plan_rejects_case_without_steps() -> None:
    with pytest.raises(plan.PlanError):
        s4_plan.build_plan(make_case(()), make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)


def test_build_plan_accepts_long_cases() -> None:
    case = make_case(tuple(step(index, "annotation_task", "提交") for index in range(1, 60)))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert len(built.steps) == 59


def test_build_plan_writes_type_value_into_target() -> None:
    case = make_case((step(1, "annotation_task", "提交"),))
    grounded = case.steps[0]
    case = replace(case, steps=(replace(grounded, kind="type",
                                        target=replace(grounded.target, value="${data.task_id}")),))

    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    assert built.steps[0].target.value == "${data.task_id}"


def test_with_status_updates_case_and_steps() -> None:
    case = make_case((step(1, "annotation_task", "提交"),))
    built = s4_plan.build_plan(case, make_graph(), SOURCE, 1, cards=make_cards(), test_data=TEST_DATA)

    confirmed = s4_plan.with_status(built, "confirmed")

    assert confirmed.status == "confirmed"
    assert {item.status for item in confirmed.steps} == {"confirmed"}
    plan.validate(confirmed)
