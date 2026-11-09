"""S3：把规范化后的步骤落到真实页面、真实元素和明确的数据上。

页面：入口页由用例菜单匹配导航图的 menu 得出，同分时用用例标题 / 菜单末段 / 步骤文字里的页面名
（全名，或 ≥3 字的前缀，如「任务池」→「任务池筛选」）打破平局。每一步的页面 =
上一步的跳转目的页 > 上一步的页面 > 入口页；只有这一步本身是跳转（kind=navigate，或文字里有
「进入 / 打开 / 前往 / 切换到 / 返回」）时才按文字找页面，而且引号里的内容、当前页知识卡里的元素名
不算页面名（「点击“关闭工作台”」不会落到标题为「工作台」的页面）。
跳转目的页 destination：本步预期里写了「进入 / 跳转到 / 返回」的那个页面，下一步继承它。
元素：只在落地页面知识卡的元素里找候选，排序是「名称完全一致 > 别名一致 > 已归档计划里用过
（source=archive）> 编辑距离相近」。唯一一个完全一致，或没有完全一致、最好的一档只有一个别名一致时
直接采用；否则用 Llm.choose 在候选里选，没有 llm 时留空。label 是 {"dynamic": ...} 的元素绝不作为候选。
goal 用落地后的真实名称改写；输入值取导图原文（第二个引号，或输入动词后的剩余文字），
没写就只说「输入内容」；手机号、密码只写 ${account.*} 引用；「任意一条 / 某个 / 待处理的」生成数据前置。
每步的 confidence 由文件顶部的纯函数 confidence_of 计算，evidence 取自 archive / source / trial。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jev_convert.compile.draft import (
    EVIDENCE_VALUES,
    GroundedCase,
    GroundedStep,
    NormalizedCase,
    NormalizedStep,
)
from jev_convert.compile.plan import Target
from jev_convert.kb.cards import CardElement, PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode, is_dynamic

if TYPE_CHECKING:
    from jev_convert.compile.llm import Llm

ELEMENT_TASK = "在页面知识卡的元素里选出这一步要操作的元素"
MATCH_RANK = {"exact": 3, "alias": 2, "fuzzy": 1, "none": 0}
FUZZY_RATIO = 0.6
MAX_CANDIDATES = 8
ENTRY_TITLE_BONUS = 5
ENTRY_PREFIX_BONUS = 3
PREFIX_PAGE_MIN = 3
# _page_in_text 的匹配档位：标题原文 > 短名做标题前缀 > 菜单段。
PAGE_LEVEL_TITLE = 3
PAGE_LEVEL_PREFIX = 2
PAGE_LEVEL_MENU = 1

# confidence_of 的权重：基础分 + 元素匹配质量 + 页面可定位程度 + 依据强度，上限正好 1.0。
CONF_BASE = 0.10
CONF_MATCH = {"exact": 0.55, "alias": 0.40, "fuzzy": 0.20, "none": 0.0}
CONF_PAGE_KNOWN = 0.05
CONF_PAGE_TITLED = 0.05
CONF_PAGE_LANDMARKED = 0.05
CONF_EVIDENCE = {"source": 0.10, "archive": 0.10, "trial": 0.05}
CONF_EVIDENCE_MAX = 0.20
CONF_DIGITS = 4

TYPE_VERBS = ("输入", "填写", "填入")
SELECT_VERBS = ("选择", "选中", "勾选")
# 「输入框 / 输入区 / 输入栏」里的「输入」不是动作。
VERB_NOT_FOLLOWED_BY = "框区栏"
VALUE_LEADS = "为是成：:，,。 "
ACCOUNT_REFS = (
    ("手机号", "${account.phone}"),
    ("电话", "${account.phone}"),
    ("密码", "${account.password}"),
    ("账号", "${account.login}"),
    ("用户名", "${account.login}"),
)
NAV_WORDS = ("进入", "跳转到", "跳转", "返回", "打开", "前往", "切换到")

REQUIREMENT_RULES = (
    ("登录", "logged_in"),
    ("权限", "has_permission"),
    ("待处理", "has_pending_task"),
    ("新版", "workspace_new_version"),
)
DATA_MARKERS = ("任意一条", "任意一个", "任意", "某个", "某条", "某一", "任一", "随机", "待处理的", "一条", "一个")
DATA_NEEDS = (
    ("待处理", "has_pending_task"),
    ("任务", "has_task"),
    ("账号", "has_account"),
    ("模型", "has_model"),
    ("数据集", "has_dataset"),
    ("批次", "has_batch"),
)

VERBS = (
    "点击", "单击", "双击", "右击", "点一下", "点开", "输入", "填入", "填写", "选择", "勾选", "选中",
    "进入", "打开", "跳转", "返回", "访问", "前往", "上传", "下载", "拖拽", "滚动", "滑动", "切换",
    "提交", "保存", "删除", "新增", "新建", "搜索", "查询", "刷新", "关闭", "取消", "等待",
)
PAGE_WORDS = (
    "页面上的", "页面中的", "列表中的", "表格中的", "页面上", "页面中", "列表里", "表格里",
    "上方的", "下方的", "右侧的", "左侧的", "里面", "中的", "里的", "上的",
)
AFFIXES = (
    "输入框", "下拉框", "下拉菜单", "下拉", "按钮", "链接", "页签", "选项卡", "标签", "图标",
    "菜单", "选项", "复选框", "单选框", "单元格", "字段", "弹窗", "对话框", "输入区", "输入", "框",
)
LEAD_CHARS = "在将把从向对于是"

_EMPTY_PAGE = NavNode(id="", path="")
_QUOTED = re.compile(r"[「『“\"']([^」』”\"']{1,40})[」』”\"']")
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[，,；;。、\n]+")
_TRIM = " \t：:，,。、；;\"'“”‘’「」『』【】《》()（）[]-—>"


def _clean(text: str) -> str:
    return _SPACE.sub("", str(text or "")).strip(_TRIM)


def _stripped(text: str) -> str:
    """去掉页面状语与「按钮 / 输入框」这类装饰后缀，得到元素名的本体。"""
    value = _clean(text)
    for word in PAGE_WORDS:
        value = value.replace(word, "")
    for affix in AFFIXES:
        if value.endswith(affix) and len(value) > len(affix):
            value = value[: -len(affix)]
            break
    return value.lstrip(LEAD_CHARS)


def _verb_index(text: str) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for verb in VERBS:
        index = text.find(verb)
        if index < 0:
            continue
        if best is None or (index, -len(verb)) < (best[0], -len(best[1])):
            best = (index, verb)
    return best


def _phrases(text: str) -> tuple[str, ...]:
    """步骤文字里的目标说法：引号内容最可靠，其次是动词前后的名词片段。"""
    found: list[str] = [match.group(1) for match in _QUOTED.finditer(text)]
    for segment in _PUNCT.split(text):
        piece = _clean(segment)
        lead = 0
        while lead < len(piece) and piece[lead] in LEAD_CHARS:
            lead += 1
        body = piece[lead:]
        if not body:
            continue
        located = _verb_index(body)
        if located is None:
            found.append(body)
            continue
        index, verb = located
        if index > 0:
            found.append(body[:index])
        rest = body[index + len(verb) :]
        if rest:
            found.append(rest)
    cleaned = [_clean(item) for item in found]
    return tuple(dict.fromkeys(item for item in cleaned if item))


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def match_kind(text: str, name: str) -> str:
    """说法与真实名称的匹配质量：exact / alias / fuzzy / none，纯函数。"""
    wanted = _clean(name)
    if not wanted or not _clean(text):
        return "none"
    phrases = _phrases(text)
    if any(_clean(phrase) == wanted for phrase in phrases):
        return "exact"
    body = _stripped(name)
    if body and any(_stripped(phrase) == body for phrase in phrases):
        return "alias"
    if any(_ratio(_stripped(phrase) or _clean(phrase), body or wanted) >= FUZZY_RATIO for phrase in phrases):
        return "fuzzy"
    return "none"


def confidence_of(step: NormalizedStep, page: NavNode, target: Target, evidence: Sequence[str]) -> float:
    """落地结果的置信度：元素匹配质量 + 页面可定位程度 + 依据强度。纯函数，便于调权重。"""
    value = CONF_BASE + CONF_MATCH[match_kind(step.text, target.name)]
    if page is not None and page.id:
        value += CONF_PAGE_KNOWN
        if page.title:
            value += CONF_PAGE_TITLED
        if page.landmarks:
            value += CONF_PAGE_LANDMARKED
    value += min(sum(CONF_EVIDENCE.get(item, 0.0) for item in evidence), CONF_EVIDENCE_MAX)
    return round(min(value, 1.0), CONF_DIGITS)


@dataclass(frozen=True)
class _Candidate:
    element: CardElement
    match: str
    ratio: float

    @property
    def label(self) -> str:
        return str(self.element.label)

    @property
    def archived(self) -> bool:
        return self.element.source == "archive"

    @property
    def key(self) -> tuple[int, int, float, str]:
        return (-MATCH_RANK[self.match], 0 if self.archived else 1, -self.ratio, self.label)


def _candidates(card: PageCard | None, text: str) -> list[_Candidate]:
    """在知识卡的元素里找候选；dynamic label 不是真实文案，直接排除。"""
    if card is None:
        return []
    found: list[_Candidate] = []
    for element in card.elements:
        label = element.label
        if not isinstance(label, str) or is_dynamic(label):
            continue
        match = match_kind(text, label)
        if match == "none":
            continue
        ratio = max(
            (_ratio(_stripped(phrase) or phrase, _stripped(label) or _clean(label)) for phrase in _phrases(text)),
            default=0.0,
        )
        found.append(_Candidate(element=element, match=match, ratio=ratio))
    found.sort(key=lambda candidate: candidate.key)
    return found[:MAX_CANDIDATES]


def _evidence(element: CardElement | None, page: NavNode | None) -> tuple[str, ...]:
    if element is None:
        return ("source",) if page is not None and page.id else ()
    source = element.source if element.source in EVIDENCE_VALUES else "source"
    return tuple(dict.fromkeys((source, "source")))


def _menu_score(node: NavNode, menu: Sequence[str]) -> int:
    common = 0
    for left, right in zip(node.menu, menu):
        if left != right:
            break
        common += 1
    if not common:
        return 0
    if common == len(node.menu) == len(menu):
        return 100
    if common == len(menu):
        return 80
    if common == len(node.menu):
        return 60
    return 40 + common


def _entry_page(graph: NavGraph, case: NormalizedCase) -> NavNode | None:
    """用例菜单 → 导航图节点；menu 完全一致优先，其次是覆盖 / 多一层功能点，最后比 title。

    同分的多个页面（同一菜单下有多个页签）用用例文字里提到的页面名打破平局：标题全文出现在用例标题 /
    步骤文字里加 ENTRY_TITLE_BONUS；用例标题 / 菜单末段 / 步骤文字里有 ≥3 字的说法是标题前缀
    （任务池 → 任务池筛选）加 ENTRY_PREFIX_BONUS。仍并列时取 id 最小者。
    """
    texts = tuple(text for text in (case.title, *(step.text for step in case.steps)) if text)
    phrases = _entry_phrases(case, texts)
    best: NavNode | None = None
    best_score = 0
    for node in sorted(graph.nodes, key=lambda item: item.id):
        score = _menu_score(node, case.menu)
        if not score and node.title and case.menu:
            if node.title == case.menu[-1]:
                score = 40
            elif node.title in case.menu:
                score = 20
        if score and node.title and len(node.title) >= 2:
            if any(node.title in text for text in texts):
                score += ENTRY_TITLE_BONUS
            elif any(node.title.startswith(phrase) for phrase in phrases):
                score += ENTRY_PREFIX_BONUS
        if score > best_score:
            best, best_score = node, score
    return best


def _entry_phrases(case: NormalizedCase, texts: Sequence[str]) -> tuple[str, ...]:
    """入口平局用的页面短名：用例标题、菜单末段、步骤文字里 ≥ PREFIX_PAGE_MIN 字的说法。"""
    found: list[str] = []
    if case.menu:
        found.append(_clean(case.menu[-1]))
    for text in texts:
        found.extend(_phrases(_clean(text)))
    return tuple(dict.fromkeys(item for item in found if len(item) >= PREFIX_PAGE_MIN))


def _quoted_spans(text: str) -> list[tuple[int, int, str]]:
    """引号里的内容所在区间（不含引号），连同内容本身。"""
    return [(match.start(1), match.end(1), match.group(1)) for match in _QUOTED.finditer(text)]


def _label_spans(text: str, labels: Sequence[str]) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for label in labels:
        if len(label) < 2:
            continue
        start = text.find(label)
        while start >= 0:
            spans.append((start, start + len(label), label))
            start = text.find(label, start + 1)
    return spans


def _free(text: str, needle: str, blocked: Sequence[tuple[int, int, str]]) -> bool:
    """needle 在 text 里至少有一处出现没有落在更长的引号内容 / 元素名里。

    引号内容或元素名恰好等于 needle 时不算遮挡（「进入“任务详情”」里的「任务详情」就是页面名）；
    「点击“关闭工作台”」里的「工作台」落在更长的「关闭工作台」里，不算页面名。
    """
    if not needle:
        return False
    start = text.find(needle)
    while start >= 0:
        end = start + len(needle)
        if not any(left <= start and end <= right and value != needle for left, right, value in blocked):
            return True
        start = text.find(needle, start + 1)
    return False


def _page_in_text(graph: NavGraph, text: str, labels: Sequence[str] = ()) -> NavNode | None:
    """步骤文字里提到的页面名：title 原文 > 短名做前缀（任务池 → 任务池筛选）> 菜单段。

    落在更长的引号内容或 labels（当前页知识卡的元素名）里的命中不算。并列时取 id 最小者，
    保证同一输入永远得到同一个页面。
    """
    # 只去空白、不剥两端引号：引号区间要保留下来做遮挡。
    clean = _SPACE.sub("", str(text or ""))
    if not _clean(clean):
        return None
    blocked = [*_quoted_spans(clean), *_label_spans(clean, [_clean(label) for label in labels])]
    phrases = [
        phrase for phrase in _phrases(clean) if len(phrase) >= PREFIX_PAGE_MIN and _free(clean, phrase, blocked)
    ]
    best: NavNode | None = None
    best_key: tuple[int, int] = (0, 0)
    for node in sorted(graph.nodes, key=lambda item: item.id):
        key: tuple[int, int] = (0, 0)
        if node.title and len(node.title) >= 2 and _free(clean, node.title, blocked):
            key = (PAGE_LEVEL_TITLE, len(node.title))
        elif node.title:
            prefixed = [len(phrase) for phrase in phrases if node.title.startswith(phrase)]
            if prefixed:
                key = (PAGE_LEVEL_PREFIX, max(prefixed))
        if key == (0, 0):
            hits = [len(segment) for segment in node.menu if len(segment) >= 2 and _free(clean, segment, blocked)]
            if hits:
                key = (PAGE_LEVEL_MENU, max(hits))
        if key > best_key:
            best, best_key = node, key
    return best


def _is_navigation(step: NormalizedStep) -> bool:
    """这一步本身是不是跳转：kind=navigate，或引号以外的文字里有「进入 / 打开 / 返回」这类动词。"""
    if step.kind == "navigate":
        return True
    bare = _QUOTED.sub("，", step.text)
    return any(word in bare for word in NAV_WORDS)


def _card_labels(card: PageCard | None) -> tuple[str, ...]:
    """知识卡里的真实元素名（dynamic 除外），用来遮挡步骤文字里的元素名。"""
    if card is None:
        return ()
    labels = [element.label for element in card.elements if isinstance(element.label, str) and element.label]
    labels += [action.target for action in card.actions if isinstance(action.target, str) and action.target]
    return tuple(dict.fromkeys(labels))


def _destination_page(graph: NavGraph, expects: Sequence[str]) -> NavNode | None:
    """这一步预期里写了「进入 / 跳转到 / 返回」的，返回预期里提到的那个页面（跳转目的页）。"""
    for text in expects:
        if any(word in text for word in NAV_WORDS):
            node = _page_in_text(graph, text)
            if node is not None:
                return node
    return None


def _context(step: NormalizedStep, page: NavNode, card: PageCard | None, found: Sequence[_Candidate]) -> dict:
    return {
        "page": page.id,
        "page_title": page.title,
        "step": step.text,
        "original": step.original,
        "kind": step.kind,
        "candidates": [
            {
                "name": candidate.label,
                "role": candidate.element.role,
                "source": candidate.element.source,
                "match": candidate.match,
            }
            for candidate in found
        ],
        "confusables": [f"{item.a} / {item.b}" for item in (card.confusables if card is not None else ())],
    }


def phrases(text: str) -> tuple[str, ...]:
    """步骤文字里的目标说法（规范化后）：确认表沉淀别名时用同一套切分。"""
    return tuple(dict.fromkeys(_clean(item) for item in _phrases(text) if _clean(item)))


def _alias_element(step: NormalizedStep, card: PageCard | None) -> CardElement | None:
    if card is None or not card.aliases:
        return None
    by_label = {element.label: element for element in card.elements if isinstance(element.label, str)}
    for phrase in phrases(step.text):
        label = card.aliases.get(phrase)
        if label in by_label:
            return by_label[label]
    return None


def _choose_element(
    llm: Llm | None, step: NormalizedStep, page: NavNode, card: PageCard | None, found: Sequence[_Candidate]
) -> CardElement | None:
    """唯一一个完全一致时直接采用；没有完全一致、最好的一档只有一个别名一致时也直接采用；
    其余（有歧义）交给 choose()，没有 llm 时留空。"""
    alias = _alias_element(step, card)
    if alias is not None:
        return alias       # 确认表里人工确认过的说法：直接复用
    if not found:
        return None
    exacts = [candidate for candidate in found if candidate.match == "exact"]
    if len(exacts) == 1:
        return exacts[0].element
    if not exacts:
        best = max(MATCH_RANK[candidate.match] for candidate in found)
        top = [candidate for candidate in found if MATCH_RANK[candidate.match] == best]
        if len(top) == 1 and top[0].match == "alias":
            return top[0].element
    if llm is None:
        return None
    labels = [candidate.label for candidate in found]
    choice = llm.choose(ELEMENT_TASK, _context(step, page, card, found), labels)
    if choice.index is None or not 0 <= choice.index < len(labels):
        return None
    return found[choice.index].element


def _rest_after_verb(text: str, name: str, verbs: Sequence[str]) -> str:
    """动作动词（输入 / 填写 / 填入）后面的原文，去掉开头的字段名与「为 / 是」，截到第一个标点。

    「输入框 / 输入区」里的「输入」不算动作。找不到动词或剩下的是空串时返回 ""。
    """
    clean = _clean(text)
    found: int | None = None
    for verb in verbs:
        start = clean.find(verb)
        while start >= 0:
            end = start + len(verb)
            if end < len(clean) and clean[end] in VERB_NOT_FOLLOWED_BY:
                start = clean.find(verb, end)
                continue
            if found is None or end < found:
                found = end
            break
    if found is None:
        return ""
    rest = _PUNCT.split(clean[found:], maxsplit=1)[0].strip(_TRIM + VALUE_LEADS)
    if name and rest.startswith(name):
        rest = rest[len(name) :].strip(_TRIM + VALUE_LEADS)
    return _clean(rest)


def _value(step: NormalizedStep, name: str = "") -> str:
    """输入 / 选择的值：手机号、密码等凭据一律只写 ${account.*} 引用（导图里写了明文也不用）；
    否则取第二个引号里的原文，再否则取动词后面的原文；导图里没写就返回 ""
    （不让运行期把占位说明当成值输入）。"""
    for word, reference in ACCOUNT_REFS:
        if word in step.text:
            return reference
    quoted = [match.group(1) for match in _QUOTED.finditer(step.text)]
    if len(quoted) >= 2:
        return _clean(quoted[1])
    verbs = SELECT_VERBS if step.kind == "select" else TYPE_VERBS
    for text in dict.fromkeys((step.text, step.original)):
        rest = _rest_after_verb(text, name, verbs)
        if rest:
            return rest
    return ""


def _goal(step: NormalizedStep, name: str, value: str) -> str:
    if not name:
        return step.text
    if step.kind == "click":
        return f"点击“{name}”"
    if step.kind == "type":
        return f"在“{name}”输入{value}" if value else f"在“{name}”输入内容"
    if step.kind == "select":
        return f"在“{name}”选择{value}" if value else f"在“{name}”选择一项"
    if step.kind == "navigate":
        return f"进入“{name}”"
    return f"等待“{name}”出现"


def _requirements(case: NormalizedCase) -> tuple[str, ...]:
    """前置条件 → requires；「任意一条 / 某个 / 待处理的」这类说法补数据前置。"""
    found: list[str] = []
    for text in case.preconditions:
        for word, need in REQUIREMENT_RULES:
            if word in text and need not in found:
                found.append(need)
    texts = (case.title, *case.preconditions, *(step.text for step in case.steps))
    for text in texts:
        if not any(marker in text for marker in DATA_MARKERS):
            continue
        for word, need in DATA_NEEDS:
            if word in text:
                if need not in found:
                    found.append(need)
                break
    return tuple(found)


def _page_title(page: NavNode | None) -> str:
    if page is None:
        return ""
    if page.title:
        return page.title
    return page.menu[-1] if page.menu else ""


def ground(
    case: NormalizedCase,
    graph: NavGraph,
    cards: Mapping[str, PageCard],
    llm: Llm | None = None,
) -> GroundedCase:
    """把每一步落到页面节点、知识卡元素与明确的数据上；落地不了就留空并降低 confidence。"""
    entry = _entry_page(graph, case)
    previous: NavNode | None = None
    arrival: NavNode | None = None
    steps: list[GroundedStep] = []
    not_automatable = not case.steps
    for position, step in enumerate(case.steps, start=1):
        # 页面 = 上一步的跳转目的页 > 上一步的页面 > 入口页；只有跳转步骤才按文字换页。
        page = arrival or previous or entry
        if _is_navigation(step):
            current = cards.get(page.id) if page is not None else None
            page = _page_in_text(graph, step.text, _card_labels(current)) or page
        destination = _destination_page(graph, step.expect)
        arrival = destination
        previous = page
        if page is None or not page.id or page.direct == "new_tab":
            not_automatable = True
        card = cards.get(page.id) if page is not None else None
        element: CardElement | None = None
        name = ""
        if step.kind == "navigate":
            name = _page_title(page)
        else:
            element = _choose_element(llm, step, page, card, _candidates(card, step.text))
            name = str(element.label) if element is not None else ""
        evidence = _evidence(element, page)
        grounded = page if page is not None else _EMPTY_PAGE
        target = Target(name=name, evidence=evidence)
        confidence = confidence_of(step, grounded, target, evidence)
        # type / select 要填的值另存一份给执行层；click / navigate / wait 没有值。
        value = _value(step, name) if step.kind in ("type", "select") else ""
        steps.append(
            GroundedStep(
                seq=position,
                source_node=step.source_node,
                original=step.original,
                page=grounded.id,
                kind=step.kind,
                goal=_goal(step, name, value),
                target=Target(name=name, confidence=confidence, evidence=evidence, value=value),
                confidence=confidence,
                evidence=evidence,
                expect=step.expect,
                destination=destination.id if destination is not None else "",
            )
        )
    entry_node = steps[0].page if steps else (entry.id if entry is not None else "")
    return GroundedCase(
        source_node=case.source_node,
        title=case.title,
        menu=case.menu,
        requires=_requirements(case),
        entry_node=entry_node,
        kb_version=graph.kb_version,
        steps=tuple(steps),
        not_automatable=not_automatable,
    )
