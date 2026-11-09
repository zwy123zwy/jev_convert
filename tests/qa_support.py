"""编译侧测试共用的计划构造器（jev-qa 的 qa_support 去掉执行侧依赖后的子集）。"""

from __future__ import annotations

from jev_convert.compile.plan import Entry, Plan, SourceRef, Step, Target

START = "点击“开始处理”"


def make_step(
    *,
    seq: int = 1,
    kind: str = "click",
    goal: str = START,
    name: str = "开始处理",
    expect: tuple[str, ...] = ("url=/task", "element=关闭工作台"),
    page: str = "pool",
    budget: int = 5,
    value: str = "",
    locator: str = "",
) -> Step:
    return Step(
        seq=seq,
        source_node=f"n_{seq}",
        original=goal,
        page=page,
        kind=kind,
        goal=goal,
        target=Target(name=name, confidence=0.95, evidence=("source",), value=value, locator=locator),
        expect=expect,
        budget=budget,
        status="confirmed",
    )


def make_plan(
    *steps: Step,
    case_id: str = "TC-pool-001",
    status: str = "confirmed",
    entry: Entry | None = None,
    destructive: bool = False,
    automatable: bool = True,
) -> Plan:
    return Plan(
        id=case_id,
        title="处理一条任务",
        status=status,
        source=SourceRef(mindnote="mn", node_id=f"node-{case_id}", version="1"),
        kb_version="webui@test",
        requires=("logged_in",),
        entry=entry or Entry(node="pool", route="url", url="/pool", arrive=("element=开始处理",)),
        steps=steps or (make_step(),),
        destructive=destructive,
        automatable=automatable,
    )
