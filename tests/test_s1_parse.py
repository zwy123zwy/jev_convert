import json
from pathlib import Path
from typing import Any

import pytest

from jev_convert.compile.llm import Choice
from jev_convert.compile.profile import ROLES, Profile, choose_profile
from jev_convert.compile.s0_read import MindNode, Mindnote, read
from jev_convert.compile.s1_parse import classify, parse, unresolved

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mindnotes"
STYLES = ("expect_as_child.json", "expect_inline.json", "extra_feature_no_precondition.json")


class FixtureLark:
    """只用 S0 的规范化：预置 resolve_wiki / mindnote_nodes 的返回。"""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def resolve_wiki(self, url: str) -> dict[str, Any]:
        return {"obj_token": self.body["token"], "obj_type": "mindnote", "version": self.body["version"]}

    def mindnote_nodes(self, token: str) -> list[dict[str, Any]]:
        return list(self.body["nodes"])


def mindnote_of(name: str, tmp_path: Path) -> Mindnote:
    body = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return read("https://x.feishu.cn/wiki/wik_1", FixtureLark(body), tmp_path)


class FakeLlm:
    """假 Llm：choose 返回固定角色（或固定编号），记录每次调用的候选与上下文。"""

    def __init__(self, role: str = "expect", index: int | None = None, unresolved: bool = False) -> None:
        self.role = role
        self.index = index
        self.unresolved = unresolved
        self.calls: list[Any] = []

    def choose(self, task: str, context: Any, candidates: Any) -> Choice:
        self.calls.append((task, dict(context), tuple(candidates)))
        if self.index is not None or self.unresolved:
            return Choice(index=self.index, reason="测试固定答案", unresolved=self.unresolved)
        return Choice(index=list(candidates).index(self.role), reason="测试固定答案")

    def propose_profiles(self, samples: Any) -> list[dict[str, Any]]:
        return []


def flat_profile(case_depth: int = 1) -> Profile:
    return Profile(
        case_depth=case_depth,
        module_depths=tuple(range(case_depth)),
        step_marker="",
        expect_marker="预期",
        precondition_keywords=("前置条件", "前置"),
        separators=(),
        scope="step",
    )


def mindnote(nodes: tuple[MindNode, ...]) -> Mindnote:
    return Mindnote(token="mn_test", version="1", nodes=nodes)


@pytest.mark.parametrize("name", STYLES)
def test_three_styles_give_the_same_case_structure(name: str, tmp_path: Path) -> None:
    mindnote_body = mindnote_of(name, tmp_path)
    profile, score = choose_profile(mindnote_body.nodes)

    drafts = parse(mindnote_body, profile)

    assert score >= 0.7
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.title == "处理一条任务"
    assert draft.menu[:3] == ("数据平台-新版", "工作台", "标注工作台")
    assert len(draft.steps) == 2
    assert [step.expect for step in draft.steps] == [("打开任务详情页",), ("提示提交成功",)]
    assert [step.source_node for step in draft.steps] == ["n_12", "n_14"]


@pytest.mark.parametrize("name", STYLES[:2])
def test_preconditions_are_recognised(name: str, tmp_path: Path) -> None:
    mindnote_body = mindnote_of(name, tmp_path)

    drafts = parse(mindnote_body, choose_profile(mindnote_body.nodes)[0])

    assert drafts[0].preconditions == ("已用测试账号登录",)


def test_without_precondition_nodes_the_list_is_empty(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("extra_feature_no_precondition.json", tmp_path)

    drafts = parse(mindnote_body, choose_profile(mindnote_body.nodes)[0])

    assert drafts[0].preconditions == ()
    assert drafts[0].menu == ("数据平台-新版", "工作台", "标注工作台", "任务处理")


def test_inline_step_keeps_the_original_text(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("expect_inline.json", tmp_path)

    drafts = parse(mindnote_body, choose_profile(mindnote_body.nodes)[0])

    assert drafts[0].steps[0].original == "进入任务池，点击「开始处理」 → 打开任务详情页"


def test_source_nodes_are_real_node_ids(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("expect_as_child.json", tmp_path)
    known = {node.id for node in mindnote_body.nodes}

    drafts = parse(mindnote_body, choose_profile(mindnote_body.nodes)[0])

    assert drafts[0].source_node in known
    for step in drafts[0].steps:
        assert step.source_node in known


def test_classify_covers_every_node(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("expect_as_child.json", tmp_path)

    roles = classify(mindnote_body, choose_profile(mindnote_body.nodes)[0])

    assert set(roles) == {node.id for node in mindnote_body.nodes}
    assert set(roles.values()) <= set(ROLES)
    assert roles["n_13"] == "expect"
    assert roles["n_11"] == "precondition"


def test_parse_is_deterministic(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("expect_as_child.json", tmp_path)
    profile = choose_profile(mindnote_body.nodes)[0]

    first = parse(mindnote_body, profile)
    second = parse(mindnote_body, profile)

    assert first == second
    assert [draft.to_dict() for draft in first] == [draft.to_dict() for draft in second]


def test_unrecognised_nodes_stay_unknown_without_llm() -> None:
    nodes = (
        MindNode("n1", None, 0, "数据平台"),
        MindNode("n2", "n1", 0, "处理一条任务"),
        MindNode("n3", "n2", 0, "点击「提交」"),
        MindNode("n4", "n3", 0, "提交后列表刷新"),
    )
    profile = flat_profile()

    drafts = parse(mindnote(nodes), profile)
    roles = classify(mindnote(nodes), profile)

    assert len(drafts) == 1
    assert drafts[0].steps[0].expect == ()
    assert roles["n4"] == "unknown"


def test_llm_assigns_role_for_unrecognised_nodes() -> None:
    nodes = (
        MindNode("n1", None, 0, "数据平台"),
        MindNode("n2", "n1", 0, "处理一条任务"),
        MindNode("n3", "n2", 0, "点击「提交」"),
        MindNode("n4", "n3", 0, "提交后列表刷新"),
    )
    drafts_llm = FakeLlm(role="expect")

    drafts = parse(mindnote(nodes), flat_profile(), drafts_llm)

    assert drafts[0].steps[0].expect == ("提交后列表刷新",)
    assert len(drafts_llm.calls) == 1
    task, context, candidates = drafts_llm.calls[0]
    assert isinstance(task, str)
    assert context["text"] == "提交后列表刷新"
    assert context["parent_role"] == "step"
    assert candidates == ROLES

    roles_llm = FakeLlm(role="expect")
    assert classify(mindnote(nodes), flat_profile(), roles_llm)["n4"] == "expect"
    assert len(roles_llm.calls) == 1


def test_llm_is_not_asked_when_the_profile_decides(tmp_path: Path) -> None:
    mindnote_body = mindnote_of("expect_as_child.json", tmp_path)
    llm = FakeLlm(role="expect")

    parse(mindnote_body, choose_profile(mindnote_body.nodes)[0], llm)

    assert llm.calls == []


@pytest.mark.parametrize("kwargs", [{"index": 99}, {"index": None, "unresolved": True}])
def test_unusable_llm_answer_keeps_unknown(kwargs: dict[str, Any]) -> None:
    nodes = (
        MindNode("n1", None, 0, "数据平台"),
        MindNode("n2", "n1", 0, "处理一条任务"),
        MindNode("n3", "n2", 0, "点击「提交」"),
        MindNode("n4", "n3", 0, "提交后列表刷新"),
    )
    llm = FakeLlm(**kwargs)

    drafts = parse(mindnote(nodes), flat_profile(), llm)

    assert drafts[0].steps[0].expect == ()
    assert classify(mindnote(nodes), flat_profile(), llm)["n4"] == "unknown"


def test_unresolved_lists_the_nodes_left_to_humans() -> None:
    nodes = (
        MindNode("n1", None, 0, "数据平台"),
        MindNode("n2", "n1", 0, "处理一条任务"),
        MindNode("n3", "n2", 0, "点击「提交」"),
        MindNode("n4", "n3", 0, "提交后列表刷新"),
    )

    assert unresolved(mindnote(nodes), flat_profile()) == ("n4",)
    assert unresolved(mindnote(nodes), flat_profile(), FakeLlm(role="expect")) == ()


def test_steps_follow_node_order() -> None:
    nodes = (
        MindNode("r", None, 0, "模块"),
        MindNode("c1", "r", 0, "用例甲"),
        MindNode("s2", "c1", 2, "点击「提交」"),
        MindNode("s1", "c1", 0, "进入任务池"),
        MindNode("s3", "c1", 5, "点击「返回」"),
    )

    drafts = parse(mindnote(nodes), flat_profile())

    assert [step.source_node for step in drafts[0].steps] == ["s1", "s2", "s3"]


def test_cases_without_steps_are_skipped() -> None:
    nodes = (
        MindNode("r", None, 0, "模块"),
        MindNode("c1", "r", 0, "只有标题的用例"),
        MindNode("c2", "r", 1, "有步骤的用例"),
        MindNode("s1", "c2", 0, "点击「提交」"),
    )

    drafts = parse(mindnote(nodes), flat_profile())

    assert [draft.source_node for draft in drafts] == ["c2"]


def test_precondition_above_the_case_applies_to_it() -> None:
    nodes = (
        MindNode("r", None, 0, "数据平台"),
        MindNode("p", "r", 0, "前置条件：已用测试账号登录"),
        MindNode("f", "r", 1, "工作台"),
        MindNode("c", "f", 0, "处理一条任务"),
        MindNode("s", "c", 0, "点击「提交」"),
        MindNode("e", "s", 0, "预期：提示提交成功"),
    )
    profile = Profile(
        case_depth=2,
        module_depths=(0, 1),
        step_marker="",
        expect_marker="预期",
        precondition_keywords=("前置条件",),
        separators=(),
        scope="step",
    )

    drafts = parse(mindnote(nodes), profile)

    assert len(drafts) == 1
    assert drafts[0].preconditions == ("已用测试账号登录",)
    assert drafts[0].menu == ("数据平台", "工作台")
    assert drafts[0].steps[0].expect == ("提示提交成功",)
