from __future__ import annotations

from dataclasses import replace

import pytest

from jev_convert.compile import s6_check
from jev_convert.compile.plan import Entry, Plan, SourceRef, Step, Target
from jev_convert.compile.s6_check import Problem, check_plan, non_automatable
from jev_convert.kb.cards import CardAssertion, CardElement, PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode

KB_VERSION = "webui@test123"
SOURCE = SourceRef(mindnote="mn1", node_id="n_10", version="1792028614")
POOL_PATH = "/workbench/annotation/task-pool"
ENTRY_URL = f"{POOL_PATH}?graph_definition_id=${{data.graph_definition_id}}&node_id=${{data.node_id}}"


def make_graph(*, direct: str = "with_params") -> NavGraph:
    nodes = (
        NavNode(
            id="annotation_task_pool",
            path=POOL_PATH,
            title="任务池",
            menu=("数据平台-新版", "工作台", "标注工作台"),
            required_query=("graph_definition_id", "node_id"),
            direct=direct,
            landmarks=("element=开始处理",),
        ),
    )
    return NavGraph(kb_version=KB_VERSION, nodes=nodes, edges=())


def make_cards() -> dict[str, PageCard]:
    return {
        "annotation_task_pool": PageCard(
            node="annotation_task_pool",
            structure_version=KB_VERSION,
            path=POOL_PATH,
            title="任务池",
            elements=(CardElement(role="button", label="开始处理"),),
            assertions=(
                CardAssertion(kind="url", value=POOL_PATH),
                CardAssertion(kind="text", value="任务池"),
                CardAssertion(kind="element", value="开始处理"),
            ),
        ),
    }


def make_step(
    *,
    page: str = "annotation_task_pool",
    name: str = "开始处理",
    expect: tuple[str, ...] = (f"url={POOL_PATH}", "element=开始处理"),
    budget: int = 10,
) -> Step:
    return Step(
        seq=1,
        source_node="n_11",
        original="进入任务池，点击“开始处理”",
        page=page,
        kind="click",
        goal="点击“开始处理”",
        target=Target(name=name, confidence=0.95, evidence=("source",)),
        expect=expect,
        budget=budget,
        status="draft",
    )


def make_plan(*, steps: tuple[Step, ...] | None = None, entry: Entry | None = None, **kwargs) -> Plan:
    return Plan(
        id="TC-annotation-task-pool-001",
        title=kwargs.pop("title", "处理一条任务"),
        status=kwargs.pop("status", "draft"),
        source=SOURCE,
        kb_version=kwargs.pop("kb_version", KB_VERSION),
        requires=("logged_in",),
        entry=entry if entry is not None else Entry(
            node="annotation_task_pool", route="url", url=ENTRY_URL, arrive=("element=开始处理",)
        ),
        steps=steps if steps is not None else (make_step(),),
        **kwargs,
    )


def wheres(problems: list[Problem]) -> list[str]:
    return [problem.where for problem in problems]


def test_clean_plan_has_no_problems() -> None:
    assert check_plan(make_plan(), make_graph(), make_cards()) == []


def test_unknown_page_is_reported() -> None:
    step = make_step(page="no_such_page", expect=("element=开始处理",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].page" in wheres(problems)
    assert any("不在导航图" in problem.message for problem in problems)


def test_element_without_card_evidence_is_reported() -> None:
    step = make_step(name="不存在的按钮", expect=("element=不存在的按钮",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].target" in wheres(problems)
    assert "steps[1].expect[0]" in wheres(problems)
    assert any("没有依据" in problem.message for problem in problems)


def test_element_not_without_card_evidence_is_reported() -> None:
    step = make_step(name="开始处理", expect=("element_not=弹窗",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].expect[0]" in wheres(problems)
    assert any("断言恒真" in problem.message for problem in problems)


def test_empty_target_name_is_reported() -> None:
    step = make_step(name="", expect=("element=开始处理",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].target" in wheres(problems)
    assert any("没有落到真实元素" in problem.message for problem in problems)


def test_text_without_card_evidence_is_reported() -> None:
    step = make_step(expect=("text=保存成功",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].expect[0]" in wheres(problems)
    assert any("查不到" in problem.message for problem in problems)


def test_url_assertion_must_match_a_page_path() -> None:
    step = make_step(expect=("url=/nowhere",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].expect[0]" in wheres(problems)
    assert any("不对应导航图" in problem.message for problem in problems)


def test_unknown_assertion_prefix_is_reported() -> None:
    step = make_step(expect=("happens=1",))

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].expect[0]" in wheres(problems)
    assert any("断言写法不认识" in problem.message for problem in problems)


def test_page_without_card_is_reported() -> None:
    problems = check_plan(make_plan(), make_graph(), {})

    assert "steps[1].page" in wheres(problems)
    assert any("没有知识卡" in problem.message for problem in problems)


def test_missing_entry_node_is_reported() -> None:
    entry = Entry(node="no_such_page", route="url", url="/x", arrive=())

    problems = check_plan(make_plan(entry=entry), make_graph(), make_cards())

    assert "entry.node" in wheres(problems)
    assert any("不在导航图" in problem.message for problem in problems)


def test_entry_url_must_keep_required_query_placeholders() -> None:
    entry = Entry(node="annotation_task_pool", route="url", url=POOL_PATH, arrive=("element=开始处理",))

    problems = check_plan(make_plan(entry=entry), make_graph(), make_cards())

    assert "entry.url" in wheres(problems)
    assert any("graph_definition_id" in problem.message for problem in problems)


def test_entry_route_must_match_page_directness() -> None:
    graph = make_graph(direct="menu")
    problems = check_plan(make_plan(), graph, make_cards())

    assert "entry.route" in wheres(problems)
    assert any("需要菜单导航" in problem.message for problem in problems)


def test_entry_menu_route_needs_menu_path() -> None:
    graph = NavGraph(
        kb_version=KB_VERSION,
        nodes=(NavNode(id="annotation_task_pool", path=POOL_PATH, direct="menu"),),
        edges=(),
    )
    entry = Entry(node="annotation_task_pool", route="menu", url=POOL_PATH, arrive=())

    problems = check_plan(make_plan(entry=entry), graph, make_cards())

    assert "entry.route" in wheres(problems)
    assert any("没有菜单路径" in problem.message for problem in problems)


def test_entry_needs_landmark_when_page_has_one() -> None:
    entry = Entry(node="annotation_task_pool", route="url", url=ENTRY_URL, arrive=())

    problems = check_plan(make_plan(entry=entry), make_graph(), make_cards())

    assert "entry.arrive" in wheres(problems)


def test_parameter_without_source_is_reported() -> None:
    step = make_step(expect=(f"url={POOL_PATH}", "element=开始处理"))
    step = replace(step, original="打开 ${unknown.value} 对应的页面")

    problems = check_plan(make_plan(steps=(step,)), make_graph(), make_cards())

    assert "steps[1].original" in wheres(problems)
    assert any("没有来源" in problem.message for problem in problems)


def test_stale_kb_version_is_reported() -> None:
    problems = check_plan(make_plan(kb_version="webui@old000"), make_graph(), make_cards())

    assert "kb_version" in wheres(problems)
    assert any("stale" in problem.message for problem in problems)


@pytest.mark.parametrize(
    "keyword",
    ["用 Canvas 画框", "页面嵌在 iframe 里", "结果在新标签页打开", "上传标注文件"],
)
def test_canvas_iframe_new_tab_upload_are_not_automatable(keyword: str) -> None:
    plan_with_feature = make_plan(title=keyword)

    problems = check_plan(plan_with_feature, make_graph(), make_cards())

    assert s6_check.AUTOMATABLE in wheres(problems)
    assert non_automatable(plan_with_feature, make_graph())


def test_new_tab_page_is_not_automatable() -> None:
    graph = NavGraph(
        kb_version=KB_VERSION,
        nodes=(
            NavNode(
                id="annotation_task_pool",
                path=POOL_PATH,
                required_query=("graph_definition_id", "node_id"),
                direct="new_tab",
                landmarks=("element=开始处理",),
            ),
        ),
        edges=(),
    )

    problems = check_plan(make_plan(), graph, make_cards())

    assert s6_check.AUTOMATABLE in wheres(problems)


def test_check_plan_never_raises_on_broken_input() -> None:
    broken = replace(make_plan(), steps=(replace(make_step(), expect=("", "url=", "element=")),))

    problems = check_plan(broken, make_graph(), make_cards())

    assert problems
    assert all(isinstance(problem, Problem) for problem in problems)


def test_problem_roundtrip() -> None:
    problem = Problem(where="steps[1].expect[0]", message="元素没有依据")

    assert Problem.from_dict(problem.to_dict()) == problem
    with pytest.raises(ValueError):
        Problem.from_dict({"where": "x", "message": "y", "extra": 1})


TASK_PATH = "/workbench/annotation/task"


def make_two_page_kb() -> tuple[NavGraph, dict[str, PageCard]]:
    """任务池 → 标注处理：换页步骤的到站地标属于目的页。"""
    graph = make_graph()
    task = NavNode(id="annotation_task", path=TASK_PATH, title="标注处理", direct="yes",
                   landmarks=("element=关闭工作台",))
    cards = make_cards()
    cards["annotation_task"] = PageCard(
        node="annotation_task",
        structure_version=KB_VERSION,
        path=TASK_PATH,
        title="标注处理",
        elements=(CardElement(role="button", label="关闭工作台"),),
        assertions=(CardAssertion(kind="url", value=TASK_PATH), CardAssertion(kind="element", value="关闭工作台")),
    )
    return replace(graph, nodes=(*graph.nodes, task)), cards


def test_page_change_landmarks_are_checked_against_the_destination_card() -> None:
    graph, cards = make_two_page_kb()
    step = make_step(expect=(f"url={TASK_PATH}", "element=关闭工作台", "text=标注处理"))
    assert check_plan(make_plan(steps=(step,)), graph, cards) == []


def test_target_is_still_checked_against_its_own_page_only() -> None:
    graph, cards = make_two_page_kb()
    step = make_step(name="关闭工作台", expect=(f"url={TASK_PATH}", "element=关闭工作台"))
    assert wheres(check_plan(make_plan(steps=(step,)), graph, cards)) == ["steps[1].target"]


def test_destination_card_is_not_used_without_a_matching_url_assertion() -> None:
    graph, cards = make_two_page_kb()
    step = make_step(expect=("element=关闭工作台",))
    assert wheres(check_plan(make_plan(steps=(step,)), graph, cards)) == ["steps[1].expect[0]"]


def test_declared_data_placeholders_have_a_source() -> None:
    from jev_convert.compile.plan import DataNeed

    step = make_step(expect=(f"url={POOL_PATH}", "element=开始处理"))
    step = replace(step, original="处理 ${task.任务名称}")
    plan = replace(make_plan(steps=(step,)), data={"task": DataNeed(source="tasks")})
    assert not [problem for problem in check_plan(plan, make_graph(), make_cards()) if "没有来源" in problem.message]
    undeclared = replace(plan, data={})
    assert any("没有来源" in problem.message for problem in check_plan(undeclared, make_graph(), make_cards()))
