from __future__ import annotations

import copy
import importlib
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jev_convert import jsonio
from jev_convert.compile import pipeline, plan
from jev_convert.compile.draft import GroundedCase, GroundedStep, NormalizedCase, NormalizedStep
from jev_convert.compile.llm import Choice
from jev_convert.compile.pipeline import CompileResult, compile_mindnote
from jev_convert.compile.plan import Target
from jev_convert.compile.profile import Profile
from jev_convert.config import load_config

URL = "https://example.feishu.cn/wiki/wikcnTEST"
KB_VERSION = "webui@test123"
POOL_PATH = "/workbench/annotation/task-pool"
TASK_PATH = "/workbench/annotation/task"
DOLLAR = chr(36)

PAYLOAD: dict[str, Any] = {
    "token": "mnTest",
    "version": "1792028614",
    "nodes": [
        {
            "node_id": "n_1",
            "text": "数据平台-新版",
            "children": [
                {
                    "node_id": "n_2",
                    "text": "工作台",
                    "children": [
                        {
                            "node_id": "n_3",
                            "text": "标注工作台",
                            "children": [
                                {
                                    "node_id": "n_10",
                                    "text": "处理一条任务",
                                    "children": [
                                        {"node_id": "n_11", "text": "前置条件：已用测试账号登录"},
                                        {
                                            "node_id": "n_12",
                                            "text": "进入任务池，点击「开始处理」",
                                            "children": [{"node_id": "n_13", "text": "预期：打开任务详情页"}],
                                        },
                                        {
                                            "node_id": "n_14",
                                            "text": "点击「提交」",
                                            "children": [{"node_id": "n_15", "text": "预期：提示提交成功"}],
                                        },
                                    ],
                                },
                                {
                                    "node_id": "n_20",
                                    "text": "审批一条指令",
                                    "children": [
                                        {
                                            "node_id": "n_21",
                                            "text": "点击「审批」",
                                            "children": [{"node_id": "n_22", "text": "预期：提示审批成功"}],
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}

PROFILE = Profile(
    case_depth=3,
    module_depths=(0,),
    step_marker="",
    expect_marker="预期",
    precondition_keywords=("前置条件",),
    separators=("，",),
    scope="step",
)

DEFAULT_STEPS = {
    "n_10": (("annotation_task_pool", "开始处理"), ("annotation_task", "提交")),
    "n_20": (("annotation_task", "审批"),),
    "n_30": (("annotation_task_pool", "开始处理"),),
}
# (用例, 步骤下标) → (S3 带出的预期原文, 跳转目的页)。没列出的步骤没有预期、不换页。
# 预期里提到的是本页的另一个元素：只点自己要点的元素、预期里什么都不写的步骤会退回 semantic=。
DEFAULT_OUTCOMES = {
    ("n_10", 0): (("出现批量处理",), ""),
    ("n_10", 1): (("显示关闭工作台",), ""),
}


class FakeLlm:
    """编译期 LLM 的替身：从不擅自改判，也不会写缓存目录。"""

    def __init__(self) -> None:
        self.choices: list[str] = []

    def choose(self, task: str, context: Any, candidates: Any) -> Choice:
        self.choices.append(task)
        return Choice(index=None, reason="假 LLM 不选", unresolved=True)

    def propose_profiles(self, samples: Any) -> list[dict[str, Any]]:
        return []


class FakeLark:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or PAYLOAD
        self.calls: list[tuple[str, ...]] = []

    def resolve_wiki(self, url: str) -> dict[str, Any]:
        self.calls.append(("resolve_wiki", url))
        return {"obj_token": self.payload["token"], "obj_type": "mindnote", "version": self.payload["version"]}

    def mindnote_nodes(self, token: str) -> list[dict[str, Any]]:
        self.calls.append(("mindnote_nodes", token))
        return self.payload["nodes"]


class FakeStages:
    """把 S2 / S3 的假实现注入 sys.modules，并记录调用顺序。"""

    def __init__(
        self,
        steps: dict[str, tuple[tuple[str, str], ...]] | None = None,
        originals: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self.steps = steps or DEFAULT_STEPS
        self.originals = originals or {}
        self.calls: list[tuple[str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeStages:
        s2 = types.ModuleType("jev_convert.compile.s2_normalize")
        s3 = types.ModuleType("jev_convert.compile.s3_ground")
        s2.normalize = self._normalize
        s3.ground = self._ground
        package = importlib.import_module("jev_convert.compile")
        monkeypatch.setitem(sys.modules, "jev_convert.compile.s2_normalize", s2)
        monkeypatch.setitem(sys.modules, "jev_convert.compile.s3_ground", s3)
        monkeypatch.setattr(package, "s2_normalize", s2, raising=False)
        monkeypatch.setattr(package, "s3_ground", s3, raising=False)
        return self

    def _normalize(self, case: Any, llm: Any) -> NormalizedCase:
        self.calls.append(("s2", case.source_node))
        return NormalizedCase(
            source_node=case.source_node,
            title=case.title,
            menu=case.menu,
            preconditions=case.preconditions,
            steps=tuple(
                NormalizedStep(source_node=item.source_node, original=item.original, text=item.original, expect=(),
                               kind="click")
                for item in case.steps
            ),
        )

    def _ground(self, normalized: NormalizedCase, graph: Any, cards: Any, llm: Any) -> GroundedCase:
        self.calls.append(("s3", normalized.source_node))
        steps = []
        for index, item in enumerate(normalized.steps):
            page, name = self.steps[normalized.source_node][index]
            confidence = 0.4 if normalized.source_node == "n_20" else 0.95
            original = self.originals.get((normalized.source_node, index), item.original)
            expect, destination = DEFAULT_OUTCOMES.get((normalized.source_node, index), ((), ""))
            steps.append(
                GroundedStep(
                    seq=index + 1,
                    source_node=item.source_node,
                    original=original,
                    page=page,
                    kind="click",
                    goal=f"点击“{name}”",
                    target=Target(name=name, confidence=confidence, evidence=("source",)),
                    confidence=confidence,
                    evidence=("source",),
                    expect=expect,
                    destination=destination,
                )
            )
        return GroundedCase(
            source_node=normalized.source_node,
            title=normalized.title,
            menu=normalized.menu,
            requires=("logged_in",),
            entry_node=self.steps[normalized.source_node][0][0],
            kb_version=graph.kb_version,
            steps=tuple(steps),
        )


def page_card(node: str, path: str, title: str, labels: tuple[str, ...]) -> dict[str, Any]:
    return {
        "node": node,
        "structure_version": KB_VERSION,
        "path": path,
        "title": title,
        "menu": [],
        "required_query": [],
        "elements": [{"role": "button", "label": label, "source": "source", "verified": 0} for label in labels],
        "actions": [
            {"kind": "click", "target": label, "source": "source", "verified": 0, "level": ""} for label in labels
        ],
        "confusables": [],
        "assertions": [
            {"kind": "url", "value": path, "source": "source", "verified": 0},
            {"kind": "text", "value": title, "source": "source", "verified": 0},
            {"kind": "element", "value": labels[0], "source": "source", "verified": 0},
        ],
        "dynamic_labels": 0,
    }


def write_kb(kb_dir: Path) -> None:
    graph = {
        "kb_version": KB_VERSION,
        "nodes": {
            "annotation_task_pool": {
                "path": POOL_PATH,
                "route_name": "AnnotationTaskPool",
                "title": "任务池",
                "backend_key": "workbench",
                "component": "TaskPool.vue",
                "menu": ["数据平台-新版", "工作台", "标注工作台"],
                "required_query": ["graph_definition_id", "node_id"],
                "direct": "with_params",
                "landmarks": ["element=开始处理"],
            },
            "annotation_task": {
                "path": TASK_PATH,
                "route_name": "AnnotationTask",
                "title": "任务详情",
                "backend_key": "workbench",
                "component": "Task.vue",
                "menu": ["数据平台-新版", "工作台", "标注工作台"],
                "required_query": [],
                "direct": "yes",
                "landmarks": ["element=提交"],
            },
        },
        "edges": [],
    }
    jsonio.dump(graph, kb_dir / "navgraph.json")
    cards = (
        page_card("annotation_task_pool", POOL_PATH, "任务池", ("开始处理", "批量处理")),
        page_card("annotation_task", TASK_PATH, "任务详情", ("提交", "关闭工作台")),
    )
    for card in cards:
        jsonio.dump(card, kb_dir / "cards" / f"{card['node']}__{KB_VERSION}.json")


@pytest.fixture
def config(tmp_path: Path) -> Any:
    template = Path(__file__).resolve().parent.parent / "qa.example.toml"
    target = tmp_path / "qa.toml"
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = load_config(target)
    data_dir = tmp_path / "data"
    write_kb(data_dir / "kb")
    return replace(loaded, paths=replace(loaded.paths, data_dir=str(data_dir)))


def run(
    config: Any,
    stages: FakeStages,
    monkeypatch: pytest.MonkeyPatch,
    *,
    until: str = "s6",
    payload: dict[str, Any] | None = None,
    progress: Any = None,
) -> CompileResult:
    stages.install(monkeypatch)
    return compile_mindnote(URL, config, until=until, llm=FakeLlm(), lark=FakeLark(payload), progress=progress)


def payload_with_extra_case_first() -> dict[str, Any]:
    """在同一页面的用例前面插入一条新用例 n_30。"""
    payload = copy.deepcopy(PAYLOAD)
    module = payload["nodes"][0]["children"][0]["children"][0]
    extra = {
        "node_id": "n_30",
        "text": "领取一条任务",
        "children": [
            {
                "node_id": "n_31",
                "text": "点击「开始处理」",
                "children": [{"node_id": "n_32", "text": "预期：打开任务详情页"}],
            }
        ],
    }
    module["children"].insert(0, extra)
    return payload


def test_compile_produces_confirmed_and_draft(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch)

    assert len(result.plans) == 2
    assert sorted(item.status for item in result.plans) == ["confirmed", "draft"]
    assert result.confirmed == 1
    assert result.draft == 1
    assert result.pending == 1
    assert result.not_automatable == 0


def test_compiled_plans_are_valid(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch)

    for item in result.plans:
        plan.validate(item)
        assert [step.seq for step in item.steps] == list(range(1, len(item.steps) + 1))
        assert all(step.expect for step in item.steps)
        assert item.kb_version == KB_VERSION
    confirmed = next(item for item in result.plans if item.status == "confirmed")
    assert confirmed.automatable is True
    assert confirmed.entry.node == "annotation_task_pool"
    assert confirmed.entry.route == "url"
    assert f"graph_definition_id={DOLLAR}{{data.graph_definition_id}}" in confirmed.entry.url


def test_case_ids_come_from_entry_page_and_source_node(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch)

    ids = {item.source.node_id: item.id for item in result.plans}
    assert ids == {
        "n_10": plan.make_case_id("annotation_task_pool", "n_10"),
        "n_20": plan.make_case_id("annotation_task", "n_20"),
    }


def test_inserting_a_case_does_not_shift_existing_ids(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    before = run(config, FakeStages(), monkeypatch)
    after = run(config, FakeStages(), monkeypatch, payload=payload_with_extra_case_first())

    old = {item.source.node_id: item.id for item in before.plans}
    new = {item.source.node_id: item.id for item in after.plans}
    assert {key: new[key] for key in old} == old
    assert len(set(new.values())) == len(new) == 3
    # n_30 与 n_10 入口页相同，id 也不撞。
    assert new["n_30"] != new["n_10"]


def test_draft_case_keeps_low_confidence(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch)

    draft = next(item for item in result.plans if item.status == "draft")
    assert draft.entry.node == "annotation_task"
    assert min(step.target.confidence for step in draft.steps) < config.thresholds.auto_confirm


def test_low_profile_fit_counts_cases_as_pending(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "choose_profile", lambda nodes, llm=None: (PROFILE, 0.1))

    result = run(config, FakeStages(), monkeypatch)

    assert result.plans == ()
    assert result.pending == 2
    assert result.confirmed == 0


def test_until_s0_stops_after_reading(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch, until="s0")

    assert result.plans == ()
    assert result.pending == 0
    source = Path(config.paths.data_dir) / "plans" / "mnTest" / "source.json"
    assert source.exists()


def test_progress_follows_stage_order(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    run(config, FakeStages(), monkeypatch, progress=seen.append)

    assert seen == ["s0", "s1", "s2"]


def test_progress_stops_where_compile_stops(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    until_s1: list[str] = []
    run(config, FakeStages(), monkeypatch, until="s1", progress=until_s1.append)

    low_fit: list[str] = []
    monkeypatch.setattr(pipeline, "choose_profile", lambda nodes, llm=None, **_: (PROFILE, 0.1))
    result = run(config, FakeStages(), monkeypatch, progress=low_fit.append)

    assert until_s1 == ["s0", "s1"]
    assert low_fit == ["s0", "s1"]      # 画像吻合度低：停在 s1，转人工标注样例
    assert result.plans == ()


def test_until_s1_skips_normalize_and_ground(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = FakeStages()

    result = run(config, stages, monkeypatch, until="s1")

    assert result.plans == ()
    assert result.pending == 2
    assert stages.calls == []


def test_until_s2_skips_ground(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = FakeStages()

    result = run(config, stages, monkeypatch, until="s2")

    assert result.plans == ()
    assert result.pending == 2
    assert [name for name, _ in stages.calls] == ["s2", "s2"]


def test_until_s3_runs_normalize_and_ground_without_plans(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = FakeStages()

    result = run(config, stages, monkeypatch, until="s3")

    assert result.plans == ()
    assert result.pending == 2
    assert [name for name, _ in stages.calls] == ["s2", "s3", "s2", "s3"]


@pytest.mark.parametrize("until", ["s4", "s5"])
def test_early_plan_stages_stay_draft(config: Any, monkeypatch: pytest.MonkeyPatch, until: str) -> None:
    result = run(config, FakeStages(), monkeypatch, until=until)

    assert len(result.plans) == 2
    assert result.confirmed == 0
    assert result.draft == 2
    assert result.pending == 2


def test_upload_step_is_not_automatable(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    stages = FakeStages(originals={("n_10", 0): "点击「上传」按钮"})

    result = run(config, stages, monkeypatch)

    assert result.not_automatable == 1
    assert result.confirmed == 0
    assert any(item.automatable is False for item in result.plans)


def test_invalid_until_is_rejected(config: Any) -> None:
    with pytest.raises(ValueError):
        compile_mindnote(URL, config, until="s9", llm=FakeLlm(), lark=FakeLark())


def test_compile_is_deterministic(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    first = run(config, FakeStages(), monkeypatch)
    second = run(config, FakeStages(), monkeypatch)

    assert [item.to_dict() for item in first.plans] == [item.to_dict() for item in second.plans]


def test_compile_result_roundtrip(config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(config, FakeStages(), monkeypatch)

    assert CompileResult.from_dict(result.to_dict()) == result
    with pytest.raises(ValueError):
        CompileResult.from_dict({"plans": []})
