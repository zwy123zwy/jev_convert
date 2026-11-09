from typing import Any

from jev_convert.compile.draft import GroundedCase, GroundedStep, NormalizedCase, NormalizedStep
from jev_convert.compile.llm import Choice
from jev_convert.compile.plan import Target
from jev_convert.compile.s3_ground import confidence_of, ground, match_kind
from jev_convert.kb.cards import CardElement, PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode

KB = "webui@abc1234"
MENU = ("数据平台-新版", "工作台", "标注工作台")


class FakeLlm:
    """假 Llm：记录候选，返回固定编号。"""

    def __init__(self, index: int | None = 0) -> None:
        self.index = index
        self.calls: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []

    def choose(self, task: str, context: dict[str, Any], candidates: tuple[str, ...]) -> Choice:
        self.calls.append((task, dict(context), tuple(candidates)))
        return Choice(index=self.index, reason="测试固定答案")


class UnusedLlm:
    """不该被问的场合用它。"""

    def choose(self, task: str, context: dict[str, Any], candidates: tuple[str, ...]) -> Choice:
        raise AssertionError(f"不该调用 choose：{task} {candidates}")


def node(ident: str, title: str, *, menu: tuple[str, ...] = MENU, direct: str = "yes",
         landmarks: tuple[str, ...] = ()) -> NavNode:
    return NavNode(id=ident, path=f"/{ident}", title=title, menu=menu, direct=direct, landmarks=landmarks)


def page(ident: str, *labels: Any, title: str = "", source: str = "source") -> PageCard:
    elements = tuple(CardElement(role="button", label=label, source=source) for label in labels)
    return PageCard(node=ident, structure_version=KB, path=f"/{ident}", title=title or ident, elements=elements)


def step(text: str, kind: str = "click", expect: tuple[str, ...] = (), source_node: str = "n_1") -> NormalizedStep:
    return NormalizedStep(source_node=source_node, original=text, text=text, expect=expect, kind=kind)


def case(*steps: NormalizedStep, title: str = "处理一条任务", menu: tuple[str, ...] = MENU,
         preconditions: tuple[str, ...] = ("已用测试账号登录",)) -> NormalizedCase:
    return NormalizedCase(source_node="n_10", title=title, menu=menu, preconditions=preconditions,
                          steps=steps)


POOL = node("task_pool", "任务池", landmarks=("element=开始处理",))
DETAIL = node("task_detail", "任务详情", menu=(*MENU, "任务处理"), landmarks=("element=提交",))
CARDS = {
    "task_pool": page("task_pool", "开始处理", "批量处理", "手机号", title="任务池"),
    "task_detail": page("task_detail", "提交", title="任务详情"),
}


def test_page_comes_from_the_menu_and_is_inherited() -> None:
    result = ground(
        case(step("进入任务池", kind="navigate"), step("点击「开始处理」")),
        NavGraph(kb_version=KB, nodes=(POOL, DETAIL)),
        CARDS,
    )

    assert [item.page for item in result.steps] == ["task_pool", "task_pool"]
    assert result.entry_node == "task_pool"
    assert result.kb_version == KB
    assert result.steps[0].target.name == "任务池"
    assert result.steps[0].goal == "进入“任务池”"


def test_step_naming_another_page_switches_page() -> None:
    result = ground(
        case(step("进入任务池", kind="navigate"), step("进入任务详情", kind="navigate")),
        NavGraph(kb_version=KB, nodes=(POOL, DETAIL)),
        CARDS,
    )

    assert [item.page for item in result.steps] == ["task_pool", "task_detail"]


def test_previous_expect_with_navigation_switches_page() -> None:
    result = ground(
        case(step("点击「开始处理」", expect=("进入任务详情页",)), step("点击「提交」")),
        NavGraph(kb_version=KB, nodes=(POOL, DETAIL)),
        CARDS,
    )

    assert [item.page for item in result.steps] == ["task_pool", "task_detail"]
    assert result.steps[1].goal == "点击“提交”"


def test_unique_exact_match_is_adopted_without_the_llm() -> None:
    result = ground(
        case(step("点击「开始处理」")),
        NavGraph(kb_version=KB, nodes=(POOL,)),
        {"task_pool": CARDS["task_pool"]},
        UnusedLlm(),
    )

    item = result.steps[0]
    assert item.target.name == "开始处理"
    assert item.goal == "点击“开始处理”"
    assert item.evidence == ("source",)
    assert item.confidence == 0.9


def test_unique_alias_match_is_adopted_without_the_llm() -> None:
    result = ground(case(step("点击开始处理按钮")), NavGraph(kb_version=KB, nodes=(POOL,)), CARDS, UnusedLlm())

    assert match_kind("点击开始处理按钮", "开始处理") == "alias"
    assert result.steps[0].target.name == "开始处理"
    assert result.steps[0].goal == "点击“开始处理”"
    assert result.steps[0].confidence == 0.75


def test_unique_alias_match_is_adopted_with_no_llm_at_all() -> None:
    result = ground(case(step("点击开始处理按钮")), NavGraph(kb_version=KB, nodes=(POOL,)), CARDS)

    assert result.steps[0].target.name == "开始处理"


def test_two_alias_matches_still_ask_the_llm() -> None:
    ambiguous = page("task_pool", "开始处理链接", "开始处理图标", title="任务池")
    llm = FakeLlm(index=1)

    result = ground(case(step("点击开始处理按钮")), NavGraph(kb_version=KB, nodes=(POOL,)),
                    {"task_pool": ambiguous}, llm)

    assert len(llm.calls) == 1
    assert {match_kind("点击开始处理按钮", label) for label in llm.calls[0][2]} == {"alias"}
    assert result.steps[0].target.name == llm.calls[0][2][1]


def test_ambiguous_alias_without_llm_stays_empty() -> None:
    ambiguous = page("task_pool", "开始处理链接", "开始处理图标", title="任务池")

    result = ground(case(step("点击开始处理按钮")), NavGraph(kb_version=KB, nodes=(POOL,)), {"task_pool": ambiguous})

    assert result.steps[0].target.name == ""


def test_multiple_candidates_are_chosen_by_the_llm() -> None:
    ambiguous = page("task_pool", "开始处理", "开始标注", title="任务池")
    llm = FakeLlm(index=1)

    result = ground(case(step("点击「开始」")), NavGraph(kb_version=KB, nodes=(POOL,)),
                    {"task_pool": ambiguous}, llm)

    assert llm.calls[0][2] == ("开始处理", "开始标注")
    assert result.steps[0].target.name == "开始标注"
    assert result.steps[0].goal == "点击“开始标注”"


def test_without_llm_ambiguous_candidates_stay_empty_and_lower_confidence() -> None:
    ambiguous = page("task_pool", "开始处理", "开始标注", title="任务池")

    result = ground(case(step("点击「开始」")), NavGraph(kb_version=KB, nodes=(POOL,)),
                    {"task_pool": ambiguous})

    assert result.steps[0].target.name == ""
    assert result.steps[0].goal == "点击「开始」"
    assert result.steps[0].confidence < 0.9


def test_archived_element_ranks_above_plain_fuzzy_match() -> None:
    elements = (
        CardElement(role="button", label="开始标注"),
        CardElement(role="button", label="开始处理", source="archive"),
    )
    card = PageCard(node="task_pool", structure_version=KB, path="/task_pool", title="任务池", elements=elements)
    llm = FakeLlm(index=0)

    result = ground(case(step("点击「开始」")), NavGraph(kb_version=KB, nodes=(POOL,)), {"task_pool": card}, llm)

    assert llm.calls[0][2] == ("开始处理", "开始标注")
    assert result.steps[0].target.name == "开始处理"
    assert result.steps[0].evidence == ("archive", "source")


def test_dynamic_label_is_never_a_candidate() -> None:
    mixed = page("task_pool", {"dynamic": "row.action"}, "批量处理", title="任务池")
    llm = FakeLlm(index=0)

    result = ground(case(step("点击「处理」")), NavGraph(kb_version=KB, nodes=(POOL,)), {"task_pool": mixed}, llm)

    assert llm.calls[0][2] == ("批量处理",)
    assert result.steps[0].target.name == "批量处理"


def test_only_dynamic_labels_leave_the_target_empty() -> None:
    dynamic_only = page("task_pool", {"dynamic": "row.action"}, title="任务池")

    result = ground(case(step("点击「处理」")), NavGraph(kb_version=KB, nodes=(POOL,)),
                    {"task_pool": dynamic_only}, UnusedLlm())

    assert result.steps[0].target.name == ""
    assert result.steps[0].confidence < 0.9


def test_confidence_is_monotone_across_match_quality() -> None:
    step_node = NormalizedStep(source_node="n_1", original="点击开始处理按钮", text="点击开始处理按钮", kind="click")
    evidence = ("source",)
    page_node = node("task_pool", "任务池", landmarks=("element=开始处理",))

    exact = confidence_of(step_node, page_node, Target(name="开始处理按钮"), evidence)
    alias = confidence_of(step_node, page_node, Target(name="开始处理"), evidence)
    fuzzy = confidence_of(step_node, page_node, Target(name="开始处理任务"), evidence)
    none = confidence_of(step_node, page_node, Target(name=""), evidence)

    assert match_kind(step_node.text, "开始处理按钮") == "exact"
    assert match_kind(step_node.text, "开始处理") == "alias"
    assert match_kind(step_node.text, "开始处理任务") == "fuzzy"
    assert match_kind(step_node.text, "") == "none"
    assert exact > alias > fuzzy > none
    assert exact == 0.9


def test_phone_is_only_a_reference_to_the_account() -> None:
    result = ground(
        case(step("在手机号输入框输入手机号", kind="type")),
        NavGraph(kb_version=KB, nodes=(POOL,)),
        {"task_pool": CARDS["task_pool"]},
    )

    item = result.steps[0]
    assert item.target.name == "手机号"
    assert item.goal == "在“手机号”输入${account.phone}"


def test_vague_step_and_login_precondition_become_requirements() -> None:
    result = ground(
        case(step("点击任意一条任务")),
        NavGraph(kb_version=KB, nodes=(POOL,)),
        CARDS,
    )

    assert "logged_in" in result.requires
    assert "has_task" in result.requires


def test_new_tab_page_is_not_automatable() -> None:
    tab = node("task_pool", "任务池", direct="new_tab")

    result = ground(case(step("点击「开始处理」")), NavGraph(kb_version=KB, nodes=(tab,)), CARDS)

    assert result.not_automatable


def test_ungrounded_page_stays_empty() -> None:
    other = node("other_page", "另一个系统", menu=("另一个系统",))

    result = ground(case(step("点击「提交」")), NavGraph(kb_version=KB, nodes=(other,)), {})

    assert [item.page for item in result.steps] == [""]
    assert result.entry_node == ""
    assert result.not_automatable
    assert result.steps[0].target.name == ""


def test_entry_tie_breaks_on_a_title_prefix() -> None:
    batch = node("batch_list", "批次列表")
    pool = node("task_pool_filter", "任务池筛选")
    result = ground(case(step("点击「开始处理」"), menu=(*MENU, "任务池")),
                    NavGraph(kb_version=KB, nodes=(batch, pool)), {})

    assert result.entry_node == "task_pool_filter"


def test_entry_prefix_shorter_than_three_chars_does_not_count() -> None:
    batch = node("batch_list", "批次列表")
    pool = node("task_pool_filter", "任务池筛选")
    result = ground(case(step("点击「开始处理」"), menu=(*MENU, "任务")),
                    NavGraph(kb_version=KB, nodes=(batch, pool)), {})

    assert result.entry_node == "batch_list"


def test_quoted_element_does_not_hijack_the_page() -> None:
    workbench = node("annotation", "工作台", menu=("数据标注模块", "数据标注", "工作台"))
    cards = {"task_pool": page("task_pool", "开始处理", "关闭工作台", title="任务池")}

    result = ground(case(step("点击“关闭工作台”")), NavGraph(kb_version=KB, nodes=(POOL, workbench)), cards)

    assert result.steps[0].page == "task_pool"
    assert result.steps[0].target.name == "关闭工作台"


def test_card_label_inside_a_navigation_step_does_not_hijack_the_page() -> None:
    workbench = node("annotation", "工作台", menu=("数据标注模块", "数据标注", "工作台"))
    cards = {"task_pool": page("task_pool", "关闭工作台", title="任务池")}

    result = ground(case(step("点击关闭工作台后返回")), NavGraph(kb_version=KB, nodes=(POOL, workbench)), cards)

    assert result.steps[0].page == "task_pool"


def test_non_navigation_step_ignores_page_names_in_its_text() -> None:
    result = ground(case(step("点击任务详情里的「开始处理」")), NavGraph(kb_version=KB, nodes=(POOL, DETAIL)), CARDS)

    assert result.steps[0].page == "task_pool"


def test_destination_comes_from_the_steps_own_expect() -> None:
    result = ground(
        case(step("点击「开始处理」", expect=("进入任务详情页",)), step("点击「提交」", expect=("提示提交成功",))),
        NavGraph(kb_version=KB, nodes=(POOL, DETAIL)),
        CARDS,
    )

    assert [item.destination for item in result.steps] == ["task_detail", ""]
    assert [item.page for item in result.steps] == ["task_pool", "task_detail"]


def test_destination_survives_round_trip_and_missing_key_reads_as_empty() -> None:
    result = ground(case(step("点击「开始处理」", expect=("进入任务详情页",))),
                    NavGraph(kb_version=KB, nodes=(POOL, DETAIL)), CARDS)
    body = result.steps[0].to_dict()

    assert body["destination"] == "task_detail"
    assert GroundedStep.from_dict(body) == result.steps[0]
    del body["destination"]
    assert GroundedStep.from_dict(body).destination == ""


def test_typed_value_comes_from_the_original_text() -> None:
    cards = {"task_pool": page("task_pool", "名称", title="任务池")}

    result = ground(case(step("在名称输入框输入任务名称", kind="type")), NavGraph(kb_version=KB, nodes=(POOL,)), cards)

    assert result.steps[0].target.name == "名称"
    assert result.steps[0].goal == "在“名称”输入任务名称"


def test_second_quote_is_the_typed_value() -> None:
    cards = {"task_pool": page("task_pool", "名称", title="任务池")}

    result = ground(case(step("在“名称”输入“任务A”", kind="type")), NavGraph(kb_version=KB, nodes=(POOL,)), cards)

    assert result.steps[0].goal == "在“名称”输入任务A"


def test_missing_typed_value_is_not_a_placeholder() -> None:
    cards = {"task_pool": page("task_pool", "名称", title="任务池")}

    result = ground(case(step("输入名称", kind="type")), NavGraph(kb_version=KB, nodes=(POOL,)), cards)

    assert result.steps[0].goal == "在“名称”输入内容"
    assert "模型" not in result.steps[0].goal


def test_quoted_phone_number_still_becomes_an_account_reference() -> None:
    result = ground(case(step("在“手机号”输入“13800000000”", kind="type")),
                    NavGraph(kb_version=KB, nodes=(POOL,)), {"task_pool": CARDS["task_pool"]})

    assert result.steps[0].goal == "在“手机号”输入${account.phone}"


def test_new_version_precondition_becomes_a_requirement() -> None:
    graph = NavGraph(kb_version=KB, nodes=(POOL,))

    for text in ("若显示旧版则切换到新版", "当前为新版"):
        result = ground(case(step("点击「开始处理」"), preconditions=(text,)), graph, CARDS)
        assert "workspace_new_version" in result.requires


def test_ground_is_deterministic_and_round_trips() -> None:
    original = case(step("进入任务池", kind="navigate"), step("点击「开始处理」"))
    graph = NavGraph(kb_version=KB, nodes=(POOL, DETAIL))

    first = ground(original, graph, CARDS, FakeLlm(index=0))
    second = ground(original, graph, CARDS, FakeLlm(index=0))

    assert first == second
    assert GroundedCase.from_dict(first.to_dict()) == first
