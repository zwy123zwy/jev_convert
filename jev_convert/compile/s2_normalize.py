"""S2：把导图里的一句话变成可执行的单个动作。

复合步骤按连接词（并 / 并且 / 然后 / 后 / ，再）与多个动词拆分，拆分与否用 Llm.choose 在
「拆」「不拆」里确认；没有 llm 时按连接词规则拆，不猜；连接词落在「合并 / 再次 / 后台」这类普通词里时不拆。
「检查 / 确认 / 查看」这类没有动作的句子并入上一步的预期，前面还没有步骤时单独成一个 wait 步骤；
「若……则……」整句转成一条前置条件（描述要达到的状态），不生成步骤；每一步标上 kind。
输入 CaseDraft，输出 NormalizedCase。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jev_convert.compile.draft import NormalizedCase, NormalizedStep
from jev_convert.compile.profile import CaseDraft, StepDraft, bare

if TYPE_CHECKING:
    from jev_convert.compile.llm import Llm

SPLIT_TASK = "确认是否把复合步骤拆成多个步骤"
SPLIT_CHOICES = ("拆", "不拆")

INLINE_MARKERS = ("—>", "→", "=>", "->", "⇒")

OPERATION_VERBS = (
    "点击", "单击", "双击", "右击", "点一下", "点开", "输入", "填入", "填写", "键入",
    "选择", "勾选", "选中", "取消勾选", "进入", "打开", "跳转", "返回", "访问", "前往",
    "上传", "下载", "拖拽", "滚动", "滑动", "切换", "提交", "保存", "删除", "新增",
    "新建", "搜索", "查询", "刷新", "导出", "导入", "关闭", "取消", "等待", "等到",
    "轮询", "稍等", "直到",
)
CHECK_VERBS = (
    "检查", "确认", "查看", "验证", "观察", "断言", "核对", "确保", "看到", "应当", "应该", "显示",
)
VERBS = tuple(sorted({*OPERATION_VERBS, *CHECK_VERBS}, key=len, reverse=True))

# 连接词：拆到「后」时要求后半句以动词开头，避免把「提交后列表刷新」拆开。
CONNECTORS = ("然后再", "然后", "接着", "随后", "之后", "以后", "并且", "并", "再", "后")
LEAD_WORDS = ("先", "再", "然后", "接着", "随后", "并且", "并", "后")
# 含连接词字符的普通词：连接词落在这些词里面时不算连接词，避免把「合并提交」拆成「合」「提交」。
WORDS_WITH_CONNECTOR = (
    "合并", "归并", "兼并", "吞并", "并发", "并列", "并行", "并排", "并集",
    "再次", "最后", "前后", "后台", "后退", "后端", "后缀", "后续", "背后", "落后",
)

KIND_WORDS = (
    ("navigate", ("进入", "打开", "跳转", "返回", "访问", "前往", "切换页面", "切换到")),
    ("type", ("输入", "填入", "填写", "键入")),
    ("select", ("选择", "勾选", "选中", "取消勾选", "下拉")),
    ("wait", ("等待", "等到", "轮询", "稍等", "直到")),
    ("click", ("点击", "单击", "双击", "右击", "点一下", "点开")),
)

# 条件写法的开头词：带「则 / 就」的用 CONDITION_WORDS，只有逗号的用 CONDITION_COMMA_WORDS。
CONDITION_WORDS = ("如果", "若", "假如", "当", "倘若", "要是")
CONDITION_COMMA_WORDS = ("如果", "若", "假如", "倘若", "要是")
# 以条件词开头但不是条件写法的词：条件句整句不生成步骤，误判会丢掉真实步骤。
NOT_CONDITION_WORDS = ("当前", "当日", "当天", "当月")

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[，,；;。、\n]+")
_TRIM = " \t：:，,。、；;"
_CONDITION = re.compile(
    r"^(?:" + "|".join(CONDITION_WORDS) + r")(?P<cond>.+?)[，,]?(?:则|就|那么|那|即|便)(?P<rest>.+)$"
)
_CONDITION_COMMA = re.compile(
    r"^(?:" + "|".join(CONDITION_COMMA_WORDS) + r")(?P<cond>.+?)[，,](?P<rest>.+)$"
)


@dataclass
class _Step:
    """拆分过程中的可变步骤；最后统一冻成 NormalizedStep。"""

    source_node: str
    original: str
    text: str
    kind: str
    expect: list[str]


def _clean(text: str) -> str:
    return _SPACE.sub(" ", str(text or "")).strip().strip(_TRIM).strip()


def _has_verb(text: str) -> bool:
    return any(verb in text for verb in VERBS)


def _has_operation(text: str) -> bool:
    return any(verb in text for verb in OPERATION_VERBS)


def _is_check(text: str) -> bool:
    return not _has_operation(text) and any(word in text for word in CHECK_VERBS)


def _strip_lead(text: str) -> str:
    rest = text.strip()
    changed = True
    while changed and rest:
        changed = False
        for word in LEAD_WORDS:
            if rest.startswith(word) and len(rest) > len(word):
                rest = rest[len(word) :].strip()
                changed = True
                break
    return rest


def _starts_with_check(text: str) -> bool:
    rest = _strip_lead(text)
    return any(rest.startswith(word) for word in CHECK_VERBS)


def _role(text: str) -> str:
    if _starts_with_check(text):
        return "check"
    if _has_operation(text):
        return "step"
    if _is_check(text):
        return "check"
    return "other"


def _starts_with_verb(text: str) -> bool:
    rest = _strip_lead(text)
    return any(rest.startswith(verb) for verb in VERBS)


def _word_spans(piece: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for word in WORDS_WITH_CONNECTOR:
        index = piece.find(word)
        while index >= 0:
            spans.append((index, index + len(word)))
            index = piece.find(word, index + 1)
    return spans


def _inside_word(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(left < end and start < right for left, right in spans)


def _split_connectors(piece: str) -> list[str]:
    """按「并 / 然后 / 后」等连接词拆；只有后半句以动词开头、且连接词不在普通词里时才认。"""
    spans = _word_spans(piece)
    for connector in CONNECTORS:
        start = 1
        while True:
            index = piece.find(connector, start)
            if index < 0:
                break
            rest = piece[index + len(connector) :]
            if not _inside_word(spans, index, index + len(connector)) and _starts_with_verb(rest):
                head = piece[:index]
                if _has_verb(head):
                    return [*_split_connectors(head), *_split_connectors(rest)]
            start = index + len(connector)
    return [piece]


def _clauses(text: str) -> list[str]:
    found: list[str] = []
    for piece in _PUNCT.split(text):
        found.extend(_split_connectors(piece.strip()))
    return [clause for clause in (_clean(piece) for piece in found) if clause]


def _strip_inline(text: str) -> str:
    """去掉行内预期（动作 → 预期）：预期的内容已经由 S1 挂在 expect 上。"""
    for marker in INLINE_MARKERS:
        index = text.find(marker)
        if index > 0:
            head = text[:index].strip(_TRIM + " ")
            if head:
                return head
    return text


def _condition(draft: StepDraft) -> str | None:
    """「若……则……」整句转成一条前置条件，不生成步骤；不是条件写法时返回 None。

    前置条件描述的是用例开始前要达到的状态，所以整句原样保留（如「若显示旧版则切换到新版」），
    不能只取条件部分（那会变成「显示旧版」，和期望的状态正好相反）。挂在这句上的预期接在「→」后面。
    """
    text = _clean(_strip_inline(bare(draft.original)))
    if text.startswith(NOT_CONDITION_WORDS):
        return None
    match = _CONDITION.match(text) or _CONDITION_COMMA.match(text)
    if match is None or not _clean(match.group("cond")) or not _clean(match.group("rest")):
        return None
    expect = [item for item in (_clean(item) for item in draft.expect) if item]
    return f"{text} → {'，'.join(expect)}" if expect else text


def _confirm_split(llm: Llm | None, draft: StepDraft, clauses: list[str]) -> bool:
    if len(clauses) < 2:
        return False
    if llm is None:
        return True
    context = {
        "source_node": draft.source_node,
        "original": draft.original,
        "clauses": list(clauses),
        "expect": list(draft.expect),
    }
    choice = llm.choose(SPLIT_TASK, context, SPLIT_CHOICES)
    if choice.index is None or not 0 <= choice.index < len(SPLIT_CHOICES):
        return True  # 没得到确认时按连接词规则拆
    return SPLIT_CHOICES[choice.index] == SPLIT_CHOICES[0]


def _kind(text: str) -> str:
    """kind 取文字里最先出现的动作词；都没有时按 click 处理。"""
    best: tuple[int, int] | None = None
    kind = "click"
    for candidate, words in KIND_WORDS:
        for word in words:
            index = text.find(word)
            if index < 0:
                continue
            key = (index, -len(word))
            if best is None or key < best:
                best = key
                kind = candidate
    return kind


def _pieces(draft: StepDraft, llm: Llm | None) -> list[tuple[str, str]]:
    """一句话 → [(角色, 文本)]；角色是 step（有动作）或 check（只描述预期）。"""
    text = _strip_inline(bare(draft.original))
    clauses = _clauses(text)
    if len(clauses) > 1 and not _confirm_split(llm, draft, clauses):
        clauses = [_clean(text)]
    pieces: list[tuple[str, str]] = []
    prefix = ""
    for clause in clauses:
        role = _role(clause)
        if role == "step":
            pieces.append(("step", _clean(prefix + clause)))
            prefix = ""
        elif role == "check":
            pieces.append(("check", _clean(prefix + "，" + clause) if prefix else clause))
            prefix = ""
        elif pieces and pieces[-1][0] == "step":
            pieces[-1] = ("step", pieces[-1][1] + "，" + clause)
        else:
            prefix = _clean(prefix + "，" + clause) if prefix else clause
    if prefix:
        pieces.append(("check", prefix))
    return pieces


def _dedup(items: list[str]) -> tuple[str, ...]:
    found: list[str] = []
    for item in items:
        text = _clean(item)
        if text and text not in found:
            found.append(text)
    return tuple(found)


def normalize(case: CaseDraft, llm: Llm | None = None) -> NormalizedCase:
    """拆复合步骤、把检查句并入预期、把条件句整句转成前置，并给每一步标 kind。

    检查句前面还没有步骤时（用例一开头就是「确认……」），单独成一个 wait 步骤，预期就是这句检查；
    不并入下一步，否则动作之前的检查会被挪到动作之后。
    """
    preconditions = list(case.preconditions)
    steps: list[_Step] = []
    for draft in case.steps:
        condition = _condition(draft)
        if condition is not None:
            preconditions.append(condition)
            continue
        for role, text in _pieces(draft, llm):
            if role == "check" and steps:
                steps[-1].expect.append(text)
                continue
            steps.append(
                _Step(
                    source_node=draft.source_node,
                    original=draft.original,
                    text=text,
                    kind="wait" if role == "check" else _kind(text),
                    expect=[text] if role == "check" else [],
                )
            )
        if draft.expect:
            if not steps:
                text = _clean(_strip_inline(bare(draft.original))) or _clean(bare(draft.original))
                steps.append(_Step(draft.source_node, draft.original, text, "wait", []))
            steps[-1].expect.extend(draft.expect)
    return NormalizedCase(
        source_node=case.source_node,
        title=case.title,
        menu=case.menu,
        preconditions=_dedup(preconditions),
        steps=tuple(
            NormalizedStep(
                source_node=step.source_node,
                original=step.original,
                text=step.text,
                expect=_dedup(step.expect),
                kind=step.kind,
            )
            for step in steps
        ),
    )
