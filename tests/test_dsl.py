from __future__ import annotations

import pytest

from jev_convert.verify.dsl import Assertion, DslError, Verdict, check, check_all, normalize, parse

OBSERVATION = {
    "url": "https://webui.test.quanta.io/workspace/annotation?tab=tasks&page=2",
    "title": "标注工作台",
    "text": "任务池 共 12 条  待标注",
    "elements": [
        {"index": 0, "label": "任务池", "role": "link", "operations": ["click"]},
        {"index": 1, "label": "任务列表", "role": "button", "operations": ["click"]},
        {"index": 2, "label": "任务详情", "role": "button", "operations": ["click"]},
        {"index": 3, "label": "标注工作台", "role": "tab", "operations": ["click"]},
        {"index": 4, "label": "确定（保存）", "role": "button", "operations": ["click"]},
    ],
    "controls": [],
    "fingerprint": None,
}


def test_parse_five_kinds():
    assert parse("url=/tasks") == Assertion(kind="url", values=("/tasks",), source="url=/tasks")
    assert parse("element=任务池") == Assertion(kind="element", values=("任务池",), source="element=任务池")
    assert parse("element_not=任务池").kind == "element_not"
    assert parse("text=共 12 条").values == ("共 12 条",)
    assert parse("semantic=页面显示待标注任务").kind == "semantic"


def test_parse_element_alternatives():
    assertion = parse("element=任务池 | 指令管理")
    assert assertion.kind == "element"
    assert assertion.values == ("任务池", "指令管理")


def test_parse_tolerates_spaces_around_prefix():
    assert parse("  url = /tasks  ").values == ("/tasks",)


@pytest.mark.parametrize("expr", ["", "   ", "element", "unknown=1", "element=", "element= | "])
def test_parse_rejects_bad_expression(expr: str):
    with pytest.raises(DslError) as caught:
        parse(expr)
    assert expr in str(caught.value)


def test_normalize_fullwidth_and_whitespace():
    assert normalize("  （标注  工作台）\n\t“A”‘B’  ") == "(标注 工作台) \"A\"'B'"
    assert normalize("任务池") == "任务池"


def test_url_passes_on_path_and_query():
    verdict = check(parse("url=/workspace/annotation?tab=tasks"), OBSERVATION)
    assert verdict == Verdict(passed=True, by="rule", actual="/workspace/annotation?tab=tasks&page=2", closest=())


def test_url_fails_when_fragment_missing():
    verdict = check(parse("url=/workspace?tab=labels"), OBSERVATION)
    assert verdict.passed is False
    assert verdict.actual == "/workspace/annotation?tab=tasks&page=2"
    assert verdict.closest == ()


def test_element_passes():
    assert check(parse("element=任务池"), OBSERVATION) == Verdict(passed=True, by="rule", actual="任务池", closest=())


def test_element_passes_after_normalize():
    assert check(parse("element=确定(保存)"), OBSERVATION).passed is True


def test_element_fails():
    verdict = check(parse("element=指令管理"), OBSERVATION)
    assert verdict.passed is False
    assert verdict.by == "rule"
    assert "任务池" in verdict.actual


def test_element_alternatives_pass_on_either_hit():
    assert check(parse("element=指令管理|任务池"), OBSERVATION).passed is True
    assert check(parse("element=指令管理|操作日志"), OBSERVATION).passed is False


def test_bar_only_splits_element_kinds():
    assert parse("text=成功|完成").values == ("成功|完成",)
    assert parse("url= /a?x=1|2 ").values == ("/a?x=1|2",)
    assert parse("semantic=是否 a|b").values == ("是否 a|b",)
    assert parse("element_not=A | B").values == ("A", "B")


def test_text_with_bar_is_taken_literally():
    observation = {**OBSERVATION, "text": "任务已完成"}
    assert check(parse("text=成功|完成"), observation).passed is False
    assert check(parse("text=完成|成功"), observation).passed is False
    assert check(parse("text=成功|完成"), {**OBSERVATION, "text": "状态：成功|完成"}).passed is True


def test_semantic_receives_the_whole_value():
    seen: list[str] = []

    def judge(original: str, visible: str) -> tuple[bool, str]:
        seen.append(original)
        return True, "ok"

    check(parse("semantic=是否 a|b"), OBSERVATION, judge)
    assert seen == ["是否 a|b"]


def test_escaped_bar_is_part_of_element_name():
    observation = {**OBSERVATION, "elements": [{"label": "A|B"}, {"label": "C"}]}
    assert parse(r"element=A\|B").values == ("A|B",)
    assert parse(r"element=A\|B | D").values == ("A|B", "D")
    assert check(parse(r"element=A\|B"), observation).passed is True
    assert check(parse("element=A|B"), observation).passed is False
    assert check(parse(r"element_not=A\|B"), observation).passed is False


def test_element_not_alternatives_mean_none_of():
    assert check(parse("element_not=指令管理|操作日志"), OBSERVATION).passed is True
    assert check(parse("element_not=指令管理|任务池"), OBSERVATION).passed is False


def test_url_matches_percent_encoded_non_ascii_query():
    observation = {**OBSERVATION, "url": "https://webui.test.quanta.io/flows?flow=%E7%AB%AF%E5%88%B0%E7%AB%AF&p=1"}
    verdict = check(parse("url=flow=端到端"), observation)
    assert verdict == Verdict(passed=True, by="rule", actual="/flows?flow=端到端&p=1", closest=())
    assert check(parse("url=flow=%E7%AB%AF%E5%88%B0%E7%AB%AF"), observation).passed is True
    assert check(parse("url=flow=其他"), observation).passed is False


def test_element_not():
    assert check(parse("element_not=指令管理"), OBSERVATION) == Verdict(
        passed=True,
        by="rule",
        actual="任务池, 任务列表, 任务详情, 标注工作台, 确定(保存)",
        closest=(),
    )
    failed = check(parse("element_not=任务池"), OBSERVATION)
    assert failed.passed is False
    assert failed.closest == ()


def test_text():
    assert check(parse("text=共 12 条"), OBSERVATION).passed is True
    assert check(parse("text=共  12  条"), OBSERVATION).passed is True
    failed = check(parse("text=已归档"), OBSERVATION)
    assert failed == Verdict(passed=False, by="rule", actual="任务池 共 12 条  待标注", closest=())


def test_semantic_uses_injected_judge():
    seen: list[tuple[str, str]] = []

    def judge(original: str, visible: str) -> tuple[bool, str]:
        seen.append((original, visible))
        return "待标注" in visible, "可见文字里有「待标注」"

    verdict = check(parse("semantic=页面显示待标注任务"), OBSERVATION, judge)
    assert verdict == Verdict(passed=True, by="semantic", actual="可见文字里有「待标注」", closest=())
    assert seen == [("页面显示待标注任务", "任务池 共 12 条  待标注")]

    denied = check(parse("semantic=页面已归档"), OBSERVATION, lambda original, visible: (False, "没看到归档字样"))
    assert denied.passed is False
    assert denied.by == "semantic"
    assert denied.closest == ()


def test_semantic_without_judge_raises():
    expr = "semantic=页面显示待标注任务"
    with pytest.raises(DslError) as caught:
        check(parse(expr), OBSERVATION)
    assert expr in str(caught.value)


def test_element_failure_reports_three_closest():
    verdict = check(parse("element=任务列"), OBSERVATION)
    assert verdict.passed is False
    assert verdict.closest == ("任务列表", "任务池", "任务详情")


def test_closest_capped_at_three_candidates():
    observation = {
        "url": "/a",
        "text": "",
        "elements": [{"label": f"按钮{index}"} for index in range(10)],
    }
    verdict = check(parse("element=按钮x"), observation)
    assert len(verdict.closest) == 3
    assert all(candidate in {f"按钮{index}" for index in range(10)} for candidate in verdict.closest)


def test_other_kinds_have_empty_closest():
    for expr in ("url=/nope", "element_not=任务池", "text=没有"):
        assert check(parse(expr), OBSERVATION).closest == ()


def test_check_all_all_pass():
    verdicts = check_all(["url=/workspace/annotation", "element=任务池", "text=待标注"], OBSERVATION)
    assert [verdict.passed for verdict in verdicts] == [True, True, True]
    assert [verdict.by for verdict in verdicts] == ["rule", "rule", "rule"]


def test_check_all_keeps_order_and_reports_each():
    exprs = ["text=已归档", "element=任务池", "url=/nope"]
    verdicts = check_all(exprs, OBSERVATION)
    assert [verdict.passed for verdict in verdicts] == [False, True, False]
    assert verdicts[2].actual == "/workspace/annotation?tab=tasks&page=2"


def test_check_all_empty():
    assert check_all([], OBSERVATION) == []


def test_check_all_propagates_syntax_error():
    with pytest.raises(DslError) as caught:
        check_all(["element=任务池", "assert=坏写法"], OBSERVATION)
    assert "assert=坏写法" in str(caught.value)


def test_check_all_accepts_judge():
    judge = lambda original, visible: (len(visible) > 0, "有文字")  # noqa: E731
    verdicts = check_all(["element=任务池", "semantic=任意"], OBSERVATION, judge)
    assert verdicts[1].by == "semantic"
    assert verdicts[1].passed is True


def test_same_input_same_output():
    first = check_all(["element=任务池", "url=/workspace/annotation", "text=待标注"], OBSERVATION)
    second = check_all(["element=任务池", "url=/workspace/annotation", "text=待标注"], OBSERVATION)
    assert first == second == check_all(["element=任务池", "url=/workspace/annotation", "text=待标注"], OBSERVATION)
    assert check(parse("element=任务列"), OBSERVATION) == check(parse("element=任务列"), OBSERVATION)


def test_assertion_roundtrip():
    assertion = parse("element=任务池|指令管理")
    assert Assertion.from_dict(assertion.to_dict()) == assertion
    with pytest.raises(DslError):
        Assertion.from_dict({**assertion.to_dict(), "extra": 1})
    with pytest.raises(DslError):
        Assertion.from_dict({"kind": "element", "values": ["任务池"]})


def test_verdict_roundtrip():
    verdict = Verdict(passed=False, by="rule", actual="任务池", closest=("任务列表",))
    assert Verdict.from_dict(verdict.to_dict()) == verdict
    assert Verdict.from_dict({"passed": True, "by": "rule", "actual": ""}).closest == ()
    with pytest.raises(DslError):
        Verdict.from_dict({"passed": True, "by": "rule", "actual": "", "why": 1})


TABLE_OBS = {
    "url": "http://localhost/list",
    "text": "任务列表",
    "elements": [],
    "tables": [{"label": "", "columns": ["任务名称", "状态"],
                "rows": [{"任务名称": "T1", "状态": "待处理"}, {"任务名称": "T2", "状态": "处理中"}]}],
    "messages": ["保存成功"],
}


def test_row_finds_a_row_by_content() -> None:
    assert check(parse("row=T2 处理中"), TABLE_OBS).passed
    assert not check(parse("row=T9"), TABLE_OBS).passed


def test_row_not_needs_a_table_on_the_page() -> None:
    assert check(parse("row_not=T9"), TABLE_OBS).passed
    assert not check(parse("row_not=T1"), TABLE_OBS).passed
    empty = {**TABLE_OBS, "tables": []}
    verdict = check(parse("row_not=T9"), empty)
    assert not verdict.passed and "没有表格" in verdict.actual


def test_text_also_matches_transient_messages() -> None:
    assert check(parse("text=保存成功"), TABLE_OBS).passed
    assert not check(parse("text=保存成功"), {**TABLE_OBS, "messages": []}).passed
