"""编译流水线（docs/implementation.md Step 12）。

S0 读图 → S1 识别用例结构 → S2 规范化 → S3 落地 → S4 组装计划 → S5 断言 → S6 静态校验，
最后按 thresholds.auto_confirm 把全部步骤都够格的用例标成 confirmed，其余留 draft。

S2、S3 由并行实现的模块提供，这里在函数内部 import：文件暂时不存在也不会让本模块 import 失败，
测试用 sys.modules 注入假实现。until 支持 s0..s6，早停时后续阶段不执行。

知识库固定在 <paths>.data_dir/kb；data_dir 是相对路径时按项目根目录解析。
增量重编译（只编译新增与修改的用例、把知识卡版本变了的计划标 stale）需要 Step 9 的计划库，
这里先不做：S6 会用 kb_version 不一致报出应该 stale 的计划。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from jev_convert import jsonio
from jev_convert.compile import plan, s0_read, s1_parse, s4_plan, s6_check
from jev_convert.compile.llm import Llm
from jev_convert.compile.profile import CaseDraft, choose_profile
from jev_convert.compile.s0_read import Mindnote
from jev_convert.config import PROJECT_ROOT, Config
from jev_convert.kb.cards import PageCard
from jev_convert.kb.load import load_cards, load_graph
from jev_convert.kb.navgraph import NavGraph
from jev_convert.lark import Lark

logger = logging.getLogger(__name__)

STAGES = ("s0", "s1", "s2", "s3", "s4", "s5", "s6")
KB_DIR = "kb"
LLM_CACHE_DIR = "llm_cache"
# build_plan 的 index 参数为保持接口契约而保留，不参与 id，这里固定传 1。
CASE_INDEX = 1


@dataclass(frozen=True)
class CompileResult:
    plans: tuple[plan.Plan, ...] = ()
    confirmed: int = 0
    draft: int = 0
    pending: int = 0
    not_automatable: int = 0
    mindnote: str = ""          # 导图 token（S0 读到的）
    fit: float = 1.0            # 写法画像吻合度；低于 thresholds.profile_fit 时不生成计划，转人工标注

    def to_dict(self) -> dict[str, Any]:
        return {
            "plans": [item.to_dict() for item in self.plans],
            "confirmed": self.confirmed,
            "draft": self.draft,
            "pending": self.pending,
            "not_automatable": self.not_automatable,
            "mindnote": self.mindnote,
            "fit": self.fit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompileResult:
        expected = ("plans", "confirmed", "draft", "pending", "not_automatable")
        unknown = sorted(set(data) - set(expected) - {"mindnote", "fit"})
        if unknown:
            raise ValueError(f"CompileResult 含未知字段：{', '.join(unknown)}")
        missing = [name for name in expected if name not in data]
        if missing:
            raise ValueError(f"CompileResult 缺少必填字段：{', '.join(missing)}")
        for name in expected[1:]:
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"CompileResult.{name} 必须是整数，实际是 {type(value).__name__}")
        return cls(
            plans=tuple(plan.Plan.from_dict(item) for item in data["plans"]),
            confirmed=data["confirmed"],
            draft=data["draft"],
            pending=data["pending"],
            not_automatable=data["not_automatable"],
            mindnote=str(data.get("mindnote", "")),
            fit=float(data.get("fit", 1.0)),
        )


def _stage(until: str) -> int:
    value = str(until).strip().lower()
    if value not in STAGES:
        raise ValueError(f"until 必须是 {'、'.join(STAGES)} 之一，实际是 {until!r}")
    return STAGES.index(value)


def _data_dir(config: Config) -> Path:
    """[paths].data_dir 是相对项目根目录的路径；绝对路径原样使用。"""
    path = Path(config.paths.data_dir)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _normalize(case: CaseDraft, llm: Llm | None):
    from jev_convert.compile import s2_normalize

    return s2_normalize.normalize(case, llm)


def _ground(normalized, graph: NavGraph, cards: Mapping[str, PageCard], llm: Llm | None):
    from jev_convert.compile import s3_ground

    return s3_ground.ground(normalized, graph, cards, llm)


def _source(mindnote: Mindnote, case: CaseDraft) -> plan.SourceRef:
    return plan.SourceRef(mindnote=mindnote.token, node_id=case.source_node, version=mindnote.version)


def _status(built: plan.Plan, problems: list[s6_check.Problem], stage: int, auto_confirm: float) -> str:
    """只有跑完 S6、没有问题、可自动化、且每一步都够置信度，才直接 confirmed。"""
    if stage < STAGES.index("s6"):
        return "draft"
    if problems or not built.automatable or not built.steps:
        return "draft"
    if min(step.target.confidence for step in built.steps) < auto_confirm:
        return "draft"
    return "confirmed"


def _mark_automatable(built: plan.Plan, problems: list[s6_check.Problem]) -> plan.Plan:
    if any(problem.where == s6_check.AUTOMATABLE for problem in problems):
        return replace(built, automatable=False)
    return built


def compile_mindnote(
    url: str,
    config: Config,
    *,
    until: str = "s6",
    llm: Llm | None = None,
    lark: Lark | None = None,
    progress: Callable[[str], None] | None = None,
) -> CompileResult:
    """把一份思维导图编译成执行计划；until 早停时只统计、不产出计划。

    progress：阶段回调，阶段开始时收到 "s0" / "s1" / "s2"（s2 起为逐用例编译，含落地、路线、断言、校验）。
    """
    stage = _stage(until)
    report = progress if progress is not None else (lambda name: None)
    data_dir = _data_dir(config)
    client = lark if lark is not None else Lark(config.lark.cli)
    report("s0")
    mindnote = s0_read.read(url, client, data_dir)
    if stage == STAGES.index("s0"):
        return CompileResult(mindnote=mindnote.token)

    graph = load_graph(data_dir / KB_DIR)
    cards = load_cards(data_dir / KB_DIR)
    model = llm if llm is not None else Llm(config, data_dir / LLM_CACHE_DIR)
    # 确认表里人工标注过的节点角色（Step 13）：只在有标注时传，保持没有标注时的调用不变
    labels = load_labels(data_dir, mindnote.token)
    extra = {"overrides": labels} if labels else {}
    report("s1")
    profile, score = choose_profile(mindnote.nodes, model, **extra)
    cases = s1_parse.parse(mindnote, profile, model, **({"labels": labels} if labels else {}))
    if score < config.thresholds.profile_fit:
        # 写法画像不够吻合：不生成计划，整批用例转人工标注样例。
        return CompileResult(pending=len(cases), mindnote=mindnote.token, fit=score)
    if stage == STAGES.index("s1"):
        return CompileResult(pending=len(cases), mindnote=mindnote.token, fit=score)

    report("s2")
    built: list[plan.Plan] = []
    failures = 0
    for case in cases:
        try:
            normalized = _normalize(case, model)
            if stage == STAGES.index("s2"):
                continue
            grounded = _ground(normalized, graph, cards, model)
            if stage <= STAGES.index("s3"):
                continue
            # id 由 plan.make_case_id(入口页, 导图节点 id) 决定：不按序号计数，导图里增删用例不会改到别的 id。
            item = s4_plan.build_plan(
                grounded,
                graph,
                _source(mindnote, case),
                CASE_INDEX,
                cards=cards,
                test_data=config.test_data,
                step_budget=config.budget.step_actions_default,
                asserts=stage >= STAGES.index("s5"),
            )
            problems = s6_check.check_plan(item, graph, cards) if stage >= STAGES.index("s6") else []
            item = _mark_automatable(item, problems)
            item = s4_plan.with_status(item, _status(item, problems, stage, config.thresholds.auto_confirm))
            if stage >= STAGES.index("s5"):
                try:
                    plan.validate(item)
                except plan.PlanError as error:
                    # 结构还不完整（例如某步没落到真实元素）：留 draft 交人工确认，不丢整条用例。
                    logger.warning("计划 %s 校验不通过，留 draft：%s", item.id, error)
                    item = s4_plan.with_status(item, "draft")
            built.append(item)
        except (plan.PlanError, ValueError, KeyError) as error:
            logger.warning("用例 %s 编译失败，计入待确认：%s", case.source_node, error)
            failures += 1
            continue

    if stage <= STAGES.index("s3"):
        # S0–S3 只走识别与落地：识别出的每条用例都还需要人工确认。
        return CompileResult(pending=len(cases) + failures, mindnote=mindnote.token, fit=score)

    confirmed = sum(1 for item in built if item.status == "confirmed")
    draft = len(built) - confirmed
    return CompileResult(
        plans=tuple(built),
        confirmed=confirmed,
        draft=draft,
        pending=draft + failures,
        not_automatable=sum(1 for item in built if not item.automatable),
        mindnote=mindnote.token,
        fit=score,
    )


def load_labels(data_dir: Path, mindnote: str) -> dict[str, str]:
    """确认表读回的写法样例标注：节点 id → 角色（qa_data/plans/<token>/labels.json）。"""
    from jev_convert.compile import library

    path = library.plans_dir(data_dir, mindnote) / library.LABELS_FILE
    if not path.exists():
        return {}
    try:
        data = jsonio.load(path)
    except (OSError, ValueError):
        return {}
    labels = data.get("labels", {}) if isinstance(data, Mapping) else {}
    return {str(key): str(value) for key, value in labels.items()}
