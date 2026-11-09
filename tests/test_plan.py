import copy
import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from jev_convert.compile.plan import (
    Entry,
    Plan,
    PlanError,
    SourceRef,
    Step,
    Target,
    make_case_id,
    make_id,
    make_id_from_node,
    validate,
)
from jev_convert.jsonio import dumps

SAMPLE: dict[str, Any] = {
    "id": "TC-workbench-taskpool-001",
    "title": "处理一条任务",
    "status": "confirmed",
    "source": {"mindnote": "HoGf...", "node_id": "n_120", "version": "1792028614"},
    "kb_version": "webui@9850351",
    "requires": ["logged_in", "has_pending_task"],
    "entry": {
        "node": "data_platform_new_annotation_task_pool",
        "route": "with_params",
        "url": "/new/data-platform/workbench/annotation/task-pool?graph_definition_id=${data.graph_definition_id}",
        "arrive": ["element=开始处理"],
    },
    "steps": [
        {
            "seq": 1,
            "source_node": "n_121",
            "original": "进入任务池，点击开始处理按钮",
            "page": "data_platform_new_annotation_task_pool",
            "kind": "click",
            "goal": "点击“开始处理”",
            "target": {"name": "开始处理", "confidence": 0.95, "evidence": ["source", "trial"], "not": []},
            "expect": ["url=/workbench/annotation/task", "element=关闭工作台"],
            "budget": 5,
            "status": "confirmed",
        }
    ],
    "teardown": [],
    "destructive": False,
    "automatable": True,
}


def sample() -> Plan:
    return Plan.from_dict(copy.deepcopy(SAMPLE))


def with_steps(plan: Plan, *steps: Step) -> Plan:
    return replace(plan, steps=steps)


def test_to_dict_matches_section_4_1() -> None:
    assert sample().to_dict() == SAMPLE


def test_target_serializes_not_key() -> None:
    target = Target(name="开始处理", confidence=0.5, evidence=("source",), not_=("已开始",))
    assert target.to_dict()["not"] == ["已开始"]
    assert "not_" not in target.to_dict()
    assert Target.from_dict({"name": "开始处理", "not": ["已开始"]}).not_ == ("已开始",)


def test_tuples_serialize_as_lists() -> None:
    body = sample().to_dict()
    assert body["requires"] == ["logged_in", "has_pending_task"]
    assert body["entry"]["arrive"] == ["element=开始处理"]
    assert body["steps"][0]["target"]["evidence"] == ["source", "trial"]
    assert body["steps"][0]["expect"] == ["url=/workbench/annotation/task", "element=关闭工作台"]


def test_roundtrip_keeps_equality() -> None:
    plan = sample()
    assert Plan.from_dict(plan.to_dict()) == plan


def test_roundtrip_with_teardown_and_flags() -> None:
    plan = sample()
    richer = replace(plan, teardown=(plan.steps[0],), destructive=True, automatable=False)
    assert Plan.from_dict(richer.to_dict()) == richer


def test_roundtrip_json_bytes_are_stable() -> None:
    plan = sample()
    first = dumps(plan.to_dict())
    second = dumps(Plan.from_dict(json.loads(first)).to_dict())
    assert first == second


def test_from_dict_rejects_unknown_top_level_field() -> None:
    body = sample().to_dict()
    body["extra"] = 1
    with pytest.raises(PlanError) as error:
        Plan.from_dict(body)
    assert "extra" in str(error.value)


def test_from_dict_rejects_missing_field() -> None:
    body = sample().to_dict()
    del body["kb_version"]
    with pytest.raises(PlanError) as error:
        Plan.from_dict(body)
    assert "kb_version" in str(error.value)


def test_from_dict_rejects_unknown_nested_field() -> None:
    body = sample().to_dict()
    body["steps"][0]["target"]["not_"] = []
    with pytest.raises(PlanError) as error:
        Plan.from_dict(body)
    assert "not_" in str(error.value)


def test_from_dict_rejects_wrong_type() -> None:
    body = sample().to_dict()
    body["steps"][0]["budget"] = "5"
    with pytest.raises(PlanError):
        Plan.from_dict(body)


def test_validate_accepts_sample() -> None:
    validate(sample())


def test_validate_rejects_bad_id() -> None:
    with pytest.raises(PlanError) as error:
        validate(replace(sample(), id="case-001"))
    assert "TC-" in str(error.value)


@pytest.mark.parametrize("status", ["done", "", "Confirmed"])
def test_validate_rejects_bad_plan_status(status: str) -> None:
    with pytest.raises(PlanError) as error:
        validate(replace(sample(), status=status))
    assert "status" in str(error.value)


def test_validate_rejects_bad_step_kind() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(with_steps(plan, replace(plan.steps[0], kind="hover")))
    assert "kind" in str(error.value)


def test_validate_rejects_bad_step_status() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(with_steps(plan, replace(plan.steps[0], status="done")))
    assert "status" in str(error.value)


def test_validate_rejects_seq_gap() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(with_steps(plan, replace(plan.steps[0], seq=2)))
    assert "seq" in str(error.value)


def test_validate_rejects_seq_not_starting_at_one() -> None:
    plan = sample()
    with pytest.raises(PlanError):
        validate(with_steps(plan, replace(plan.steps[0], seq=0)))


def test_validate_rejects_empty_expect() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(with_steps(plan, replace(plan.steps[0], expect=())))
    assert "expect" in str(error.value)


def test_large_step_budget_is_no_longer_rejected() -> None:
    """55 步上限只为避开 jev-ultrafast 的 60 步限制，新执行层已删除（agent-design 12.4）。"""
    plan = sample()
    validate(with_steps(plan, replace(plan.steps[0], budget=500)))


def test_target_locator_and_value_round_trip_and_are_omitted_when_empty() -> None:
    plan = sample()
    step = plan.steps[0]
    assert "locator" not in step.target.to_dict() and "value" not in step.target.to_dict()
    target = replace(step.target, locator="button|开始处理|page|0", value="${data.task_id}")
    assert Target.from_dict(target.to_dict()) == target

def test_validate_accepts_many_steps_without_a_total_budget() -> None:
    plan = sample()
    steps = tuple(replace(plan.steps[0], seq=index, budget=26) for index in range(1, 41))
    validate(with_steps(plan, *steps))


def test_validate_rejects_empty_steps() -> None:
    with pytest.raises(PlanError) as error:
        validate(replace(sample(), steps=()))
    assert "步骤" in str(error.value)


@pytest.mark.parametrize("kb_version", ["9850351", "webui@", "webui-9850351", "kb@9850351"])
def test_validate_rejects_bad_kb_version(kb_version: str) -> None:
    with pytest.raises(PlanError) as error:
        validate(replace(sample(), kb_version=kb_version))
    assert "kb_version" in str(error.value)


def test_validate_accepts_kb_version_with_dirty_suffix() -> None:
    validate(replace(sample(), kb_version="webui@9850351-dirty"))


def test_validate_rejects_empty_source_node() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(replace(plan, source=replace(plan.source, node_id="")))
    assert "node_id" in str(error.value)


def test_validate_rejects_empty_entry_url() -> None:
    plan = sample()
    with pytest.raises(PlanError) as error:
        validate(replace(plan, entry=replace(plan.entry, url="")))
    assert "entry.url" in str(error.value)


def test_make_id_uses_last_ascii_segment() -> None:
    assert make_id(("data-platform", "workbench"), 1) == "TC-workbench-001"
    assert make_id(("Data Platform", "Task Pool"), 12) == "TC-task-pool-012"


def test_make_id_is_stable() -> None:
    assert make_id(("a", "workbench"), 3) == make_id(("a", "workbench"), 3)


def test_make_id_splits_camel_case() -> None:
    assert make_id(["annotation", "taskPool"], 7) == "TC-task-pool-007"


def test_make_id_falls_back_when_menu_is_chinese_only() -> None:
    assert make_id(("数据平台-新版", "工作台", "标注工作台"), 1) == "TC-case-001"


def test_make_id_rejects_zero_index() -> None:
    with pytest.raises(PlanError):
        make_id(("workbench",), 0)


def test_constructors_accept_keyword_arguments() -> None:
    plan = Plan(
        id="TC-workbench-001",
        title="处理一条任务",
        status="draft",
        source=SourceRef(mindnote="HoGf", node_id="n_1", version="1"),
        kb_version="webui@9850351",
        requires=(),
        entry=Entry(node="task_pool", route="yes", url="/task-pool"),
        steps=(
            Step(
                seq=1,
                source_node="n_2",
                original="点击开始处理",
                page="task_pool",
                kind="click",
                goal="点击“开始处理”",
                target=Target(name="开始处理"),
                expect=("element=关闭工作台",),
                budget=5,
                status="draft",
            ),
        ),
    )
    validate(plan)
    assert Plan.from_dict(plan.to_dict()) == plan

def test_make_id_from_node_uses_tail_segments() -> None:
    node = "data_platform_new_annotation_task_pool"
    assert make_id_from_node(node, 1) == "TC-annotation-task-pool-001"


def test_make_case_id_uses_entry_tail_and_source_hash() -> None:
    node = "data_platform_new_annotation_task_pool_filter"
    digest = hashlib.sha1(b"n_120").hexdigest()[:6]

    assert make_case_id(node, "n_120") == f"TC-task-pool-filter-{digest}"
    assert make_case_id(node, "n_120") == make_case_id(node, "n_120")


def test_make_case_id_separates_cases_on_the_same_page() -> None:
    node = "quality_review_inspection"

    assert make_case_id(node, "n_1") != make_case_id(node, "n_2")
    assert make_case_id("graph_node", "n_1").startswith("TC-graph-node-")


def test_make_case_id_rejects_empty_inputs() -> None:
    with pytest.raises(PlanError):
        make_case_id("", "n_1")
    with pytest.raises(PlanError):
        make_case_id("graph_node", " ")


def test_make_id_from_node_is_stable_and_rejects_bad_index() -> None:
    assert make_id_from_node("graph_node", 2) == "TC-graph-node-002"
    with pytest.raises(PlanError):
        make_id_from_node("graph_node", 0)
    with pytest.raises(PlanError):
        make_id_from_node("", 1)


def test_data_and_produces_round_trip_and_are_omitted_when_empty() -> None:
    from jev_convert.compile.plan import DataNeed

    plan = replace(sample(), data={"task": DataNeed(source="tasks", state="待处理", consume=True,
                                                    with_group=("状态!=处理中",))},
                   produces={"task": "处理中"})
    data = plan.to_dict()
    assert data["data"] == {"task": {"source": "tasks", "state": "待处理", "consume": True,
                                     "with_group": ["状态!=处理中"]}}
    assert Plan.from_dict(data) == plan
    assert "data" not in sample().to_dict() and "produces" not in sample().to_dict()
    validate(plan)


def test_target_row_round_trips() -> None:
    step = replace(sample().steps[0], target=Target(name="开始处理", row="${task.任务名称}"))
    plan = with_steps(sample(), step)
    assert Plan.from_dict(plan.to_dict()) == plan


def test_produces_must_name_declared_data() -> None:
    with pytest.raises(PlanError, match="produces"):
        validate(replace(sample(), produces={"task": "处理中"}))


def test_data_name_cannot_shadow_other_placeholders() -> None:
    from jev_convert.compile.plan import DataNeed

    with pytest.raises(PlanError, match="data 的名字"):
        validate(replace(sample(), data={"account": DataNeed(source="tasks")}))
