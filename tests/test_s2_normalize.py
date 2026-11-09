import pytest

from jev_convert.compile.draft import NormalizedCase
from jev_convert.compile.llm import Choice
from jev_convert.compile.profile import CaseDraft, StepDraft
from jev_convert.compile.s2_normalize import SPLIT_CHOICES, normalize

MENU = ("数据平台-新版", "工作台", "标注工作台")


class FakeLlm:
    """假 Llm：记录每次调用的候选与上下文，返回固定编号。"""

    def __init__(self, index: int | None = 0, unresolved: bool = False) -> None:
        self.index = index
        self.unresolved = unresolved
        self.calls: list[tuple[str, dict, tuple[str, ...]]] = []

    def choose(self, task: str, context: dict, candidates: tuple[str, ...]) -> Choice:
        self.calls.append((task, dict(context), tuple(candidates)))
        return Choice(index=self.index, reason="测试固定答案", unresolved=self.unresolved)


class UnusedLlm:
    """不该被问的场合用它：一旦调用就让测试失败。"""

    def choose(self, task: str, context: dict, candidates: tuple[str, ...]) -> Choice:
        raise AssertionError(f"不该调用 choose：{task} {candidates}")


def case(*steps: StepDraft, preconditions: tuple[str, ...] = ("已用测试账号登录",)) -> CaseDraft:
    return CaseDraft(
        source_node="n_10",
        title="处理一条任务",
        menu=MENU,
        preconditions=preconditions,
        steps=steps,
    )


def test_composite_step_is_split_on_connector_and_verbs() -> None:
    draft = case(StepDraft("n_12", "进入任务池，点击「开始处理」", ("打开任务详情页",)))

    result = normalize(draft)

    assert [step.text for step in result.steps] == ["进入任务池", "点击「开始处理」"]
    assert [step.kind for step in result.steps] == ["navigate", "click"]
    assert [step.expect for step in result.steps] == [(), ("打开任务详情页",)]
    assert [step.source_node for step in result.steps] == ["n_12", "n_12"]
    assert [step.original for step in result.steps] == ["进入任务池，点击「开始处理」"] * 2


def test_split_is_confirmed_by_choose() -> None:
    draft = case(StepDraft("n_12", "进入任务池，点击「开始处理」", ()))
    llm = FakeLlm(index=0)

    result = normalize(draft, llm)

    assert len(result.steps) == 2
    assert len(llm.calls) == 1
    _task, context, candidates = llm.calls[0]
    assert candidates == SPLIT_CHOICES
    assert context["clauses"] == ["进入任务池", "点击「开始处理」"]
    assert context["original"] == "进入任务池，点击「开始处理」"


def test_choose_can_keep_the_step_whole() -> None:
    draft = case(StepDraft("n_12", "进入任务池，点击「开始处理」", ("打开任务详情页",)))

    result = normalize(draft, FakeLlm(index=SPLIT_CHOICES.index("不拆")))

    assert len(result.steps) == 1
    assert result.steps[0].text == "进入任务池，点击「开始处理」"
    assert result.steps[0].kind == "navigate"
    assert result.steps[0].expect == ("打开任务详情页",)


def test_unconfirmed_split_falls_back_to_the_connector_rule() -> None:
    draft = case(StepDraft("n_12", "进入任务池，点击「开始处理」", ()))

    result = normalize(draft, FakeLlm(index=None, unresolved=True))

    assert [step.text for step in result.steps] == ["进入任务池", "点击「开始处理」"]


def test_single_clause_is_not_sent_to_the_llm() -> None:
    draft = case(StepDraft("n_14", "点击「提交」", ("提示提交成功",)))

    result = normalize(draft, UnusedLlm())

    assert len(result.steps) == 1
    assert result.steps[0].text == "点击「提交」"


def test_check_only_step_merges_into_the_previous_expect() -> None:
    draft = case(
        StepDraft("n_14", "点击「提交」", ("提示提交成功",)),
        StepDraft("n_15", "检查列表里出现新记录", ()),
    )

    result = normalize(draft)

    assert len(result.steps) == 1
    assert result.steps[0].expect == ("提示提交成功", "检查列表里出现新记录")
    assert all("检查" not in step.text for step in result.steps)


def test_leading_check_becomes_a_wait_step_before_the_action() -> None:
    draft = case(
        StepDraft("n_11", "确认提交成功", ()),
        StepDraft("n_12", "点击保存", ()),
        StepDraft("n_13", "检查列表里出现新记录", ()),
    )

    result = normalize(draft)

    assert [step.text for step in result.steps] == ["确认提交成功", "点击保存"]
    assert [step.kind for step in result.steps] == ["wait", "click"]
    assert [step.expect for step in result.steps] == [("确认提交成功",), ("检查列表里出现新记录",)]
    assert [step.source_node for step in result.steps] == ["n_11", "n_12"]


def test_check_clause_inside_a_composite_step_becomes_expect() -> None:
    draft = case(StepDraft("n_14", "点击「提交」，确认提示提交成功", ()))

    result = normalize(draft)

    assert [step.text for step in result.steps] == ["点击「提交」"]
    assert result.steps[0].expect == ("确认提示提交成功",)


def test_condition_becomes_one_precondition_without_a_step() -> None:
    draft = case(
        StepDraft("n_11", "若显示旧版则切换到新版", ()),
        StepDraft("n_12", "点击「开始处理」", ()),
    )

    result = normalize(draft)

    assert result.preconditions == ("已用测试账号登录", "若显示旧版则切换到新版")
    assert [step.text for step in result.steps] == ["点击「开始处理」"]
    assert [step.kind for step in result.steps] == ["click"]
    assert "显示旧版" not in result.preconditions


def test_condition_written_with_a_comma_is_also_a_precondition() -> None:
    draft = case(
        StepDraft("n_12", "如果账号没有权限，点击「申请」", ()),
        StepDraft("n_13", "点击「提交」", ()),
        preconditions=(),
    )

    result = normalize(draft)

    assert result.preconditions == ("如果账号没有权限，点击「申请」",)
    assert [step.text for step in result.steps] == ["点击「提交」"]


def test_condition_keeps_its_expect_in_the_precondition() -> None:
    draft = case(StepDraft("n_12", "若任务列表为空则点击「刷新」", ("列表出现任务",)), preconditions=())

    result = normalize(draft)

    assert result.preconditions == ("若任务列表为空则点击「刷新」 → 列表出现任务",)
    assert result.steps == ()


def test_words_starting_with_a_condition_word_are_not_conditions() -> None:
    result = normalize(case(StepDraft("n_12", "当前页面点击「就绪」按钮", ()), preconditions=()))

    assert result.preconditions == ()
    assert [step.text for step in result.steps] == ["当前页面点击「就绪」按钮"]


def test_repeated_preconditions_are_kept_once() -> None:
    draft = case(
        StepDraft("n_12", "若任务列表为空则点击「刷新」", ()),
        StepDraft("n_13", "若任务列表为空则点击「刷新」", ()),
        StepDraft("n_14", "点击「提交」", ()),
    )

    result = normalize(draft)

    assert result.preconditions == ("已用测试账号登录", "若任务列表为空则点击「刷新」")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("点击合并按钮", ["点击合并按钮"]),
        ("选择后台管理菜单", ["选择后台管理菜单"]),
        ("点击再次提交按钮", ["点击再次提交按钮"]),
        ("点击合并提交按钮", ["点击合并提交按钮"]),
        ("选择归并删除", ["选择归并删除"]),
        ("点击前后切换按钮", ["点击前后切换按钮"]),
        ("滚动到最后点击加载更多", ["滚动到最后点击加载更多"]),
        ("点击保存然后台账刷新", ["点击保存然后台账刷新"]),
        ("输入名称并点击提交", ["输入名称", "点击提交"]),
        ("勾选后保存", ["勾选", "保存"]),
        ("进入后台后点击设置", ["进入后台", "点击设置"]),
    ],
)
def test_connector_inside_a_word_does_not_split(text: str, expected: list[str]) -> None:
    result = normalize(case(StepDraft("n_12", text, ())))

    assert [step.text for step in result.steps] == expected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("进入任务池", "navigate"),
        ("输入手机号", "type"),
        ("选择任务类型", "select"),
        ("等待列表加载完成", "wait"),
        ("点击「提交」", "click"),
    ],
)
def test_kind_is_taken_from_the_leading_verb(text: str, kind: str) -> None:
    result = normalize(case(StepDraft("n_12", text, ())))

    assert result.steps[0].kind == kind


def test_inline_expect_is_removed_from_the_step_text() -> None:
    draft = case(StepDraft("n_12", "点击「提交」 → 提示提交成功", ("提示提交成功",)))

    result = normalize(draft)

    assert [step.text for step in result.steps] == ["点击「提交」"]
    assert result.steps[0].expect == ("提示提交成功",)
    assert result.steps[0].original == "点击「提交」 → 提示提交成功"


def test_step_numbering_is_stripped() -> None:
    result = normalize(case(StepDraft("n_12", "1. 点击「提交」", ())))

    assert result.steps[0].text == "点击「提交」"


def test_case_without_any_action_still_has_one_step() -> None:
    result = normalize(case(StepDraft("n_12", "检查页面已加载", ()), preconditions=()))

    assert len(result.steps) == 1
    assert result.steps[0].text == "检查页面已加载"
    assert result.steps[0].kind == "wait"


def test_normalize_is_deterministic_and_round_trips() -> None:
    draft = case(
        StepDraft("n_12", "进入任务池，点击「开始处理」", ("打开任务详情页",)),
        StepDraft("n_14", "点击「提交」", ("提示提交成功",)),
    )

    first = normalize(draft, FakeLlm(index=0))
    second = normalize(draft, FakeLlm(index=0))

    assert first == second
    assert NormalizedCase.from_dict(first.to_dict()) == first
