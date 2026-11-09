"""S6：计划落地前的静态校验（docs/implementation.md Step 12）。

检查项：页面节点存在、入口可达、元素与断言在知识卡里有依据、参数有来源，
以及涉及 Canvas / iframe / 新标签页 / 文件上传的用例（这类标 automatable=false）。
一律返回 list[Problem]，不抛异常：校验器自己出错也当成一条问题返回。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from jev_convert.compile import plan
from jev_convert.compile.s5_assert import card_labels, card_texts
from jev_convert.kb.cards import PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode
from jev_convert.verify.dsl import DslError
from jev_convert.verify.dsl import parse as parse_assertion

AUTOMATABLE = "automatable"
ROUTES = ("url", "menu")
PLACEHOLDER = re.compile(r"[$]{([^{}]*)}")
PLACEHOLDER_ROOTS = ("data.", "account.")
# 关键词一律按小写匹配：canvas / iframe 这类英文按原样，中文不受大小写影响。
MANUAL_FEATURES = (
    ("canvas", ("canvas", "画布")),
    ("iframe", ("iframe", "内嵌页", "嵌入页")),
    ("new_tab", ("新标签页", "新窗口", "另开", "新开标签")),
    ("upload", ("上传", "导入文件", "选择文件")),
)


@dataclass(frozen=True)
class Problem:
    where: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"where": self.where, "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Problem:
        unknown = sorted(set(data) - {"where", "message"})
        if unknown:
            raise ValueError(f"Problem 含未知字段：{', '.join(unknown)}")
        missing = [name for name in ("where", "message") if name not in data]
        if missing:
            raise ValueError(f"Problem 缺少必填字段：{', '.join(missing)}")
        if not isinstance(data["where"], str) or not isinstance(data["message"], str):
            raise ValueError("Problem 的 where 与 message 必须是字符串")
        return cls(where=data["where"], message=data["message"])


def _nodes(graph: NavGraph) -> dict[str, NavNode]:
    return {node.id: node for node in graph.nodes}


def _known_url(fragment: str, nodes: Mapping[str, NavNode]) -> bool:
    """url 片段要能和导航图里某个页面的 path 对上（带 query 的只比 path 部分）。"""
    needle = fragment.split("?", 1)[0].strip()
    if not needle:
        return False
    return any(needle == node.path or needle in node.path for node in nodes.values())


def _check_placeholders(where: str, text: str, problems: list[Problem],
                        roots: Sequence[str] = PLACEHOLDER_ROOTS) -> None:
    """参数要有来源：认 ${data.xxx}、${account.xxx}，以及计划 data 里声明过的 ${<数据名>.<列名>}；xxx 不能为空。"""
    for match in PLACEHOLDER.finditer(text or ""):
        name = match.group(1).strip()
        root = next((item for item in roots if name.startswith(item)), "")
        if not root or name == root:
            declared = "、".join(f"${{{item}xxx}}" for item in roots)
            problems.append(Problem(where, f"参数引用 {match.group(0)} 没有来源：只认 {declared}"))


def _check_entry(plan: plan.Plan, nodes: Mapping[str, NavNode], problems: list[Problem],
                 roots: Sequence[str] = PLACEHOLDER_ROOTS) -> None:
    entry = plan.entry
    node = nodes.get(entry.node)
    if node is None:
        problems.append(Problem("entry.node", f"入口页 {entry.node} 不在导航图里"))
        return
    if entry.route not in ROUTES:
        problems.append(Problem("entry.route", f"入口 route 必须是 {' / '.join(ROUTES)}，实际是 {entry.route!r}"))
        return
    if entry.route == "url":
        if node.direct == "menu":
            problems.append(Problem("entry.route", f"入口页 {node.id} 需要菜单导航（direct=menu），不能直接打开 url"))
        if node.direct == "new_tab":
            problems.append(
                Problem("entry.route", f"入口页 {node.id} 会开新标签页（direct=new_tab），不能直接打开 url")
            )
        # 必需参数可以来自 test_data（${data.x}），也可以来自领到的数据（${task.列名}）。
        missing = [key for key in node.required_query
                   if not re.search(rf"(?:[?&]){re.escape(key)}=\$\{{[^}}]+\}}", entry.url)]
        if missing:
            problems.append(Problem("entry.url", f"入口 url 缺少必需参数的占位：{', '.join(missing)}"))
    elif not node.menu:
        problems.append(Problem("entry.route", f"入口 route=menu，但 {node.id} 在导航图里没有菜单路径"))
    if node.landmarks and not entry.arrive:
        problems.append(Problem("entry.arrive", f"入口缺少到站地标断言：{node.id} 有地标 {list(node.landmarks)}"))
    _check_placeholders("entry.url", entry.url, problems, roots)


def _check_expr(
    expr: str,
    where: str,
    card: PageCard | None,
    labels: Sequence[str],
    texts: Sequence[str],
    nodes: Mapping[str, NavNode],
    problems: list[Problem],
    roots: Sequence[str] = PLACEHOLDER_ROOTS,
) -> None:
    try:
        assertion = parse_assertion(expr)
    except DslError as error:
        problems.append(Problem(where, f"断言写法不认识：{expr!r}（{error}）"))
        return
    _check_placeholders(where, expr, problems, roots)
    if assertion.kind == "url":
        if not _known_url(assertion.values[0], nodes):
            problems.append(Problem(where, f"url 片段 {assertion.values[0]!r} 不对应导航图里的任何页面：{expr!r}"))
        return
    if card is None:
        return
    if assertion.kind in ("element", "element_not"):
        for value in assertion.values:
            if value in labels:
                continue
            hint = "，断言恒真" if assertion.kind == "element_not" else ""
            problems.append(
                Problem(where, f"{assertion.kind} 的元素“{value}”在页面 {card.node} 的知识卡里没有依据{hint}：{expr!r}")
            )
        return
    if assertion.kind == "text":
        for value in assertion.values:
            if value not in texts:
                problems.append(Problem(where, f"文字“{value}”在页面 {card.node} 的知识卡里查不到：{expr!r}"))


def _destinations(step: plan.Step, nodes: Mapping[str, NavNode]) -> tuple[str, ...]:
    """这一步 url= 断言指向的页面：path 与导航图节点完全一致的才算（换页步骤的目的页）。"""
    found: list[str] = []
    for expr in step.expect:
        try:
            assertion = parse_assertion(expr)
        except DslError:
            continue
        if assertion.kind != "url":
            continue
        path = unquote(assertion.values[0].split("?", 1)[0].strip()).rstrip("/")
        for node in sorted(nodes.values(), key=lambda item: item.id):
            if path and node.path.rstrip("/") == path and node.id != step.page:
                found.append(node.id)
    return tuple(dict.fromkeys(found))


def _check_step(
    step: plan.Step,
    where: str,
    nodes: Mapping[str, NavNode],
    cards: Mapping[str, PageCard],
    problems: list[Problem],
    roots: Sequence[str] = PLACEHOLDER_ROOTS,
) -> None:
    card = cards.get(step.page)
    if step.page not in nodes:
        problems.append(Problem(f"{where}.page", f"页面节点 {step.page} 不在导航图里"))
    elif card is None:
        problems.append(Problem(f"{where}.page", f"页面 {step.page} 没有知识卡，元素与断言的依据无从校验"))
    # target 只在本页找依据；断言还可以在目的页找（换页步骤的到站地标属于目的页）。
    target_labels = card_labels(card)
    extra = [cards[node_id] for node_id in _destinations(step, nodes) if node_id in cards]
    labels = tuple(dict.fromkeys((*target_labels, *(label for item in extra for label in card_labels(item)))))
    texts = tuple(dict.fromkeys((*card_texts(card), *(text for item in extra for text in card_texts(item)))))
    expect_card = card if card is not None else (extra[0] if extra else None)

    name = step.target.name.strip()
    if not name:
        problems.append(Problem(f"{where}.target", "target.name 为空：这一步没有落到真实元素"))
    elif step.kind == "navigate":
        node = nodes.get(step.page)
        titles = {node.title.strip()} if node is not None else set()
        if name not in titles and name not in target_labels:
            problems.append(
                Problem(f"{where}.target", f"跳转目标“{name}”既不等于页面 {step.page} 的标题，也不是它的元素")
            )
    elif card is not None and name not in target_labels:
        problems.append(Problem(f"{where}.target", f"元素“{name}”在页面 {step.page} 的知识卡里没有依据"))
    for index, expr in enumerate(step.expect):
        _check_expr(expr, f"{where}.expect[{index}]", expect_card, labels, texts, nodes, problems, roots)
    _check_placeholders(f"{where}.goal", step.goal, problems, roots)
    _check_placeholders(f"{where}.original", step.original, problems, roots)
    _check_placeholders(f"{where}.target.value", step.target.value, problems, roots)
    _check_placeholders(f"{where}.target.row", step.target.row, problems, roots)


def non_automatable(plan: plan.Plan, graph: NavGraph) -> tuple[str, ...]:
    """列出该计划涉及、但目前自动化不了的能力（Canvas、iframe、新标签页、文件上传）。"""
    nodes = _nodes(graph)
    texts = [plan.title, plan.entry.url]
    for step in (*plan.steps, *plan.teardown):
        texts += [step.original, step.goal, step.target.name]
    haystack = " ".join(texts).lower()

    reasons: list[str] = []
    for feature, keywords in MANUAL_FEATURES:
        hit = next((keyword for keyword in keywords if keyword in haystack), "")
        if hit:
            reasons.append(f"涉及 {feature}（命中“{hit}”）")
    for node_id in (plan.entry.node, *(step.page for step in plan.steps)):
        node = nodes.get(node_id)
        if node is not None and node.direct == "new_tab":
            reasons.append(f"页面 {node.id} 只能在新标签页打开（direct=new_tab）")
            break
    return tuple(dict.fromkeys(reasons))


def check_plan(plan: plan.Plan, graph: NavGraph, cards: Mapping[str, PageCard]) -> list[Problem]:
    """静态校验一份计划；返回全部问题，不抛异常。"""
    problems: list[Problem] = []
    try:
        nodes = _nodes(graph)
        if plan.kb_version != graph.kb_version:
            problems.append(
                Problem("kb_version", f"计划依据 {plan.kb_version}，导航图是 {graph.kb_version}：应标 stale 后重编")
            )
        roots = (*PLACEHOLDER_ROOTS, *(f"{name}." for name in sorted(plan.data)))
        _check_entry(plan, nodes, problems, roots)
        for position, step in enumerate(plan.steps, start=1):
            _check_step(step, f"steps[{position}]", nodes, cards, problems, roots)
        for position, step in enumerate(plan.teardown, start=1):
            _check_step(step, f"teardown[{position}]", nodes, cards, problems, roots)
        for reason in non_automatable(plan, graph):
            problems.append(Problem(AUTOMATABLE, f"{reason}：应标 automatable=false"))
    except Exception as error:  # 静态校验不抛异常：结构异常也当成一条问题返回
        problems.append(Problem("plan", f"静态校验中断：{type(error).__name__}: {error}"))
    return problems
