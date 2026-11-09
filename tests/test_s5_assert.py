from __future__ import annotations

from jev_convert.compile import s5_assert
from jev_convert.compile.draft import GroundedStep
from jev_convert.compile.plan import Target
from jev_convert.kb.cards import CardAction, CardAssertion, CardElement, PageCard
from jev_convert.verify.dsl import parse as parse_assertion

PATH = "/workbench/annotation/task"


def card(*, labels: tuple[str, ...] = ("关闭工作台", "提交"), messages: tuple[str, ...] = ("提交成功",)) -> PageCard:
    return PageCard(
        node="annotation_task",
        structure_version="webui@test123",
        path=PATH,
        title="任务详情",
        elements=tuple(CardElement(role="button", label=label) for label in labels),
        actions=tuple(CardAction(kind="message", target=text) for text in messages),
        assertions=(
            CardAssertion(kind="url", value=PATH),
            CardAssertion(kind="text", value="任务详情"),
            CardAssertion(kind="element", value=labels[0] if labels else ""),
        ),
    )


POOL_PATH = "/workbench/annotation/task-pool"


def pool_card() -> PageCard:
    return PageCard(
        node="task_pool",
        structure_version="webui@test123",
        path=POOL_PATH,
        title="任务池",
        elements=(CardElement(role="button", label="开始处理"),),
        assertions=(CardAssertion(kind="url", value=POOL_PATH), CardAssertion(kind="element", value="开始处理")),
    )


def step(
    *,
    name: str = "提交",
    original: str = "点击“提交”",
    goal: str = "点击“提交”",
    kind: str = "click",
    page: str = "annotation_task",
    destination: str = "",
    expect: tuple[str, ...] = (),
) -> GroundedStep:
    return GroundedStep(
        seq=1,
        source_node="n_12",
        original=original,
        page=page,
        kind=kind,
        goal=goal,
        target=Target(name=name, confidence=0.9),
        confidence=0.9,
        evidence=("source",),
        expect=expect,
        destination=destination,
    )


def test_navigation_step_uses_url_and_landmark_of_the_destination() -> None:
    item = step(name="开始处理", original="点击“开始处理”", page="task_pool", destination="annotation_task",
                expect=("跳转到任务详情页",))

    expect = s5_assert.step_expect(item, pool_card(), card())

    assert expect == (f"url={PATH}", "element=关闭工作台")
    assert f"url={POOL_PATH}" not in expect


def test_navigation_words_without_destination_emit_no_url() -> None:
    expect = s5_assert.step_expect(step(kind="navigate", original="打开详情页", expect=("进入详情页",)), card())

    assert not any(expr.startswith("url=") for expr in expect)


def test_destination_equal_to_own_page_emits_no_url() -> None:
    item = step(destination="annotation_task", expect=("返回任务详情",))

    expect = s5_assert.step_expect(item, card(), card())

    assert not any(expr.startswith("url=") for expr in expect)


def test_message_step_uses_text_from_card() -> None:
    expect = s5_assert.step_expect(step(name="提交成功", original="点击“提交”，提示“提交成功”"), card())

    assert expect == ("text=提交成功",)


def test_message_without_card_text_falls_through() -> None:
    expect = s5_assert.step_expect(step(name="提交", original="点击“提交”，提示“保存失败”"), card())

    assert expect == ("semantic=点击“提交”，提示“保存失败”",)


def test_own_target_is_never_an_element_assertion() -> None:
    expect = s5_assert.step_expect(step(name="提交", expect=("出现提交",)), card())

    assert "element=提交" not in expect
    assert expect == ("semantic=点击“提交”",)


def test_element_mentioned_in_expect_uses_element_assertion() -> None:
    expect = s5_assert.step_expect(step(name="提交", expect=("出现关闭工作台按钮",)), card())

    assert expect == ("element=关闭工作台",)


def test_element_from_destination_card_counts() -> None:
    item = step(name="提交", destination="task_pool", expect=("可以看到开始处理",))
    no_url = PageCard(node="task_pool", structure_version="webui@test123", path=POOL_PATH, title="任务池",
                      elements=(CardElement(role="button", label="开始处理"),))

    assert s5_assert.step_expect(item, card(), no_url) == ("element=开始处理",)


def test_element_disappearance_uses_element_not() -> None:
    expect = s5_assert.step_expect(step(name="提交", expect=("弹窗消失",)), card())

    assert expect == ("element_not=提交",)


def test_unknown_element_falls_back_to_semantic() -> None:
    expect = s5_assert.step_expect(step(name="任务状态", original="检查任务状态"), card())

    assert expect == ("semantic=检查任务状态",)


def test_step_without_card_falls_back_to_semantic() -> None:
    assert s5_assert.step_expect(step(name="提交"), None) == ("semantic=点击“提交”",)


def test_step_without_target_name_uses_goal_as_semantic() -> None:
    expect = s5_assert.step_expect(step(name="", goal="等待任务结束", original=""), None)

    assert expect == ("semantic=等待任务结束",)


def test_every_expect_is_valid_dsl() -> None:
    steps = (
        step(name="开始处理", page="task_pool", destination="annotation_task", expect=("跳转到任务详情页",)),
        step(name="提交成功", original="提示“提交成功”"),
        step(name="提交", expect=("出现关闭工作台",)),
        step(name="提交", expect=("弹窗消失",)),
        step(name="任务状态", original="检查任务状态"),
        step(name="", original=""),
    )

    for item in steps:
        for expr in s5_assert.step_expect(item, card(), card()):
            assert parse_assertion(expr).kind in ("url", "element", "element_not", "text", "semantic")


def test_incoming_text_is_converted_to_dsl() -> None:
    values = ("提示提交成功", "url=/workbench/annotation/task", "打开了另一个页面")

    assert s5_assert.incoming_expects(values, card()) == (
        "text=提交成功",
        "url=/workbench/annotation/task",
        "semantic=打开了另一个页面",
    )


def test_step_expect_is_deterministic() -> None:
    item = step(name="提交", original="点击“提交”，提示“提交成功”")
    page = card()

    assert s5_assert.step_expect(item, page) == s5_assert.step_expect(item, page)


def test_dynamic_card_labels_are_not_used_as_text() -> None:
    page = PageCard(
        node="annotation_task",
        structure_version="webui@test123",
        path=PATH,
        elements=(CardElement(role="button", label={"dynamic": "item.label"}),),
        assertions=(CardAssertion(kind="url", value=PATH),),
    )

    expect = s5_assert.step_expect(step(name="item", original="点击 item"), page)

    assert expect == ("semantic=点击 item",)
    assert s5_assert.card_labels(page) == ()
