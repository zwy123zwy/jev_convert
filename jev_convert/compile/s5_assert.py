"""S5：为每一步生成断言（docs/implementation.md Step 12）。

优先级：页面跳转 → url= + 地标；提示文字 → text=（在知识卡的 assertions / ElMessage 文案里查得到才用）；
元素出现 / 消失 → element= / element_not=；都不行才 semantic=。

- 页面跳转只看 S3 给的 destination（这一步执行后应到达的页面）：url= 与地标都取自**目的页**的知识卡；
  没有 destination，或 destination 就是本步所在页时，不写 url=（否则断言在点击前就成立）。
- element=X 只写预期原文里提到、且在本页或目的页知识卡里查得到的元素，而且 X 不能是本步要操作的
  元素（它在点击前就在，断言恒真）；预期里写了「消失 / 关闭」时允许 element_not=<本步元素>。
规则断言都写不出来时退回 semantic=，调用方（s4.build_plan）据此把该步 confidence 压到 0.5。
"""

from __future__ import annotations

from collections.abc import Sequence

from jev_convert.compile.draft import GroundedStep
from jev_convert.kb.cards import PageCard
from jev_convert.verify.dsl import DslError
from jev_convert.verify.dsl import parse as parse_assertion

MESSAGE_KEYWORDS = ("提示", "弹出", "显示", "报错", "错误提示", "提示语", "校验")
DISAPPEAR_KEYWORDS = ("消失", "不再", "关闭", "隐藏", "清除", "移除")
SEMANTIC_FALLBACK = "页面状态符合预期"


def card_labels(card: PageCard | None) -> tuple[str, ...]:
    """知识卡里能当真实文案用的元素名：dynamic 标签是源码绑定表达式，不算。"""
    if card is None:
        return ()
    labels = [element.label for element in card.elements if isinstance(element.label, str) and element.label.strip()]
    labels += [action.target for action in card.actions if isinstance(action.target, str) and action.target.strip()]
    return tuple(dict.fromkeys(labels))


def card_texts(card: PageCard | None) -> tuple[str, ...]:
    """知识卡里能当可见文字用的文案：页面标题、text 断言、ElMessage 文案。"""
    if card is None:
        return ()
    texts = [assertion.value for assertion in card.assertions
             if assertion.kind == "text" and isinstance(assertion.value, str) and assertion.value.strip()]
    texts += [action.target for action in card.actions
              if action.kind == "message" and isinstance(action.target, str) and action.target.strip()]
    if card.title.strip():
        texts.append(card.title)
    return tuple(dict.fromkeys(texts))


def _step_text(step: GroundedStep) -> str:
    return f"{step.original} {step.goal}".strip()


def _has(text: str, keywords: Sequence[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _navigation_expect(card: PageCard | None) -> tuple[str, ...]:
    """页面跳转：url= + 到站地标，两样都取自目标页知识卡的 assertions。"""
    if card is None:
        return ()
    urls = [item.value for item in card.assertions
            if item.kind == "url" and isinstance(item.value, str) and item.value.strip()]
    if not urls:
        return ()
    landmarks = [item.value for item in card.assertions
                 if item.kind == "element" and isinstance(item.value, str) and item.value.strip()]
    return (f"url={urls[0].strip()}", *(f"element={value.strip()}" for value in landmarks))


def _message_expect(step: GroundedStep, card: PageCard | None, text: str) -> tuple[str, ...]:
    """提示文字：导图原文里出现的文案，且在知识卡里查得到，才写成 text=。"""
    if card is None or not _has(text, MESSAGE_KEYWORDS):
        return ()
    found = [item for item in card_texts(card) if item in text]
    return tuple(f"text={item}" for item in found)


def _element_names(*cards: PageCard | None) -> tuple[str, ...]:
    """知识卡里真实的元素名（只看 elements，dynamic 除外），按卡片顺序去重。"""
    names: list[str] = []
    for card in cards:
        if card is None:
            continue
        names += [element.label.strip() for element in card.elements
                  if isinstance(element.label, str) and element.label.strip()]
    return tuple(dict.fromkeys(names))


def _mentioned(names: Sequence[str], text: str) -> tuple[str, ...]:
    """预期原文里提到的元素名；被更长的、也提到了的元素名包含的短名不算（处理 ⊂ 处理已领取任务）。"""
    hits = [name for name in names if len(name) >= 2 and name in text]
    return tuple(name for name in hits if not any(name != other and name in other for other in hits))


def _element_expect(
    step: GroundedStep, card: PageCard | None, destination_card: PageCard | None, expect_text: str
) -> tuple[str, ...]:
    """元素出现 / 消失：只依据预期原文，不拿本步要操作的元素写 element=（点击前就在，恒真）。"""
    name = step.target.name.strip()
    names = _element_names(card, destination_card)
    mentioned = _mentioned(names, expect_text)
    # 「关闭工作台」这类元素名里的「关闭」不是消失：先去掉提到的元素名再找消失关键词。
    bare = expect_text
    for item in sorted(mentioned, key=len, reverse=True):
        bare = bare.replace(item, "，")
    if _has(bare, DISAPPEAR_KEYWORDS):
        if name and card is not None and name in card_labels(card):
            return (f"element_not={name}",)
        return ()
    return tuple(f"element={item}" for item in mentioned if item != name)


def _semantic_value(step: GroundedStep) -> str:
    for value in (step.original, step.goal, step.target.name):
        if value.strip():
            return value.strip()
    return SEMANTIC_FALLBACK


def incoming_expects(values: Sequence[str], card: PageCard | None) -> tuple[str, ...]:
    """S3 带来的原文预期可能不是 dsl 表达式：能对上知识卡文案就写 text=，否则退回 semantic=。"""
    converted: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            parse_assertion(text)
        except DslError:
            found = [item for item in card_texts(card) if item in text or text in item]
            if found:
                converted.extend(f"text={item}" for item in found)
            else:
                converted.append(f"semantic={text}")
        else:
            converted.append(text)
    return tuple(dict.fromkeys(converted))


def _changes_page(step: GroundedStep) -> bool:
    return bool(step.destination) and step.destination != step.page


def step_expect(
    step: GroundedStep, card: PageCard | None, destination_card: PageCard | None = None
) -> tuple[str, ...]:
    """按优先级为一步生成断言；返回的每一条都是 dsl 表达式。

    card 是本步所在页的知识卡，destination_card 是 step.destination 的知识卡（不换页时传 None）。
    """
    target = destination_card if _changes_page(step) else None
    if target is not None:
        rules = _navigation_expect(target)
        if rules:
            return rules
    rules = _message_expect(step, card, _step_text(step))
    if rules:
        return rules
    rules = _element_expect(step, card, target, " ".join(str(item) for item in step.expect))
    if rules:
        return rules
    return (f"semantic={_semantic_value(step)}",)
