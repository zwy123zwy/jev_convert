"""断言 DSL：把一条 expect 表达式编译成可复现的判定。

七种写法（详见 docs/interfaces.md 的 dsl 小节）：url、element、element_not、text、row、row_not、semantic。
除 semantic 外都是纯规则判定，semantic 交给注入的 judge。判定过程无 I/O、无全局状态。

row=<文字>：页面表格里有一行包含这段文字（按数据身份找行，例如 row=${task.任务名称}）。
row_not=<文字>：页面上至少有一个表格，且没有任何一行包含这段文字。没有表格时不通过：
列表加载失败、还没渲染出来时，“这一行不在了”不能算成立。
text= 同时查可见文字和动作期间出现过的短暂提示（observation.messages）。

「|」只在 element / element_not 里有特殊含义：element=A|B 表示任一命中，element_not=A|B 表示都不出现。
元素名本身含「|」时写成「\\|」，例如 element=A\\|B 断言名为「A|B」的元素。
url、text、semantic 的取值整段保留（只去首尾空白），「|」按普通字符处理。
url 断言比较前对观察到的 path+query 和片段都做 percent 解码，所以 url=flow=端到端 能匹配 %E7%AB%AF…。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

KINDS = ("url", "element", "element_not", "text", "row", "row_not", "semantic")
ALTERNATIVE_KINDS = ("element", "element_not")
CLOSEST_LIMIT = 3
ROW_SAMPLE = 5

_FULLWIDTH = str.maketrans({"（": "(", "）": ")", "“": '"', "”": '"', "‘": "'", "’": "'"})
_SPACES = re.compile(r"\s+")
_UNESCAPED_BAR = re.compile(r"(?<!\\)\|")


class DslError(ValueError):
    """断言表达式非法，或缺少判定所需的信息。信息里带原表达式。"""


@dataclass(frozen=True)
class Assertion:
    kind: str
    values: tuple[str, ...]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "values": list(self.values), "source": self.source}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Assertion:
        _reject_unknown(data, ("kind", "values", "source"), cls.__name__)
        kind = _require(data, "kind", cls.__name__)
        values = _require(data, "values", cls.__name__)
        if not isinstance(values, (list, tuple)) or not all(isinstance(item, str) for item in values):
            raise DslError(f"{cls.__name__}.values 必须是字符串列表，实际是 {values!r}")
        if kind not in KINDS:
            raise DslError(f"{cls.__name__}.kind 不在 {KINDS} 内，实际是 {kind!r}")
        return cls(kind=kind, values=tuple(values), source=str(_require(data, "source", cls.__name__)))


@dataclass(frozen=True)
class Verdict:
    passed: bool
    by: str
    actual: str
    closest: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "by": self.by, "actual": self.actual, "closest": list(self.closest)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Verdict:
        _reject_unknown(data, ("passed", "by", "actual", "closest"), cls.__name__)
        closest = data.get("closest", ())
        if not isinstance(closest, (list, tuple)) or not all(isinstance(item, str) for item in closest):
            raise DslError(f"{cls.__name__}.closest 必须是字符串列表，实际是 {closest!r}")
        return cls(
            passed=bool(_require(data, "passed", cls.__name__)),
            by=str(_require(data, "by", cls.__name__)),
            actual=str(_require(data, "actual", cls.__name__)),
            closest=tuple(closest),
        )


def _require(data: Mapping[str, Any], key: str, owner: str) -> Any:
    if key not in data:
        raise DslError(f"{owner} 缺少字段 {key}")
    return data[key]


def _reject_unknown(data: Mapping[str, Any], known: Sequence[str], owner: str) -> None:
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise DslError(f"{owner} 出现未知字段 {unknown}")


def parse(expr: str) -> Assertion:
    """把 "url=/x" 这样的表达式解析成 Assertion。语法错误抛 DslError。"""
    source = expr if isinstance(expr, str) else str(expr)
    text = source.strip()
    if not text:
        raise DslError(f"断言表达式为空：{source!r}")
    prefix, sep, rest = text.partition("=")
    kind = prefix.strip().lower()
    if not sep or kind not in KINDS:
        raise DslError(f"无法识别的断言写法 {source!r}：前缀必须是 {'、'.join(KINDS)} 之一，例如 url=/tasks")
    if kind in ALTERNATIVE_KINDS:
        parts = (part.replace("\\|", "|").strip() for part in _UNESCAPED_BAR.split(rest))
        values = tuple(part for part in parts if part)
    else:
        values = (rest.strip(),) if rest.strip() else ()
    if not values:
        raise DslError(f"断言表达式缺少取值：{source!r}")
    return Assertion(kind=kind, values=values, source=source)


def normalize(name: str) -> str:
    """去首尾空白、合并连续空白、全角括号与引号转半角。不做模糊匹配。"""
    return _SPACES.sub(" ", str(name).translate(_FULLWIDTH)).strip()


def check(
    assertion: Assertion,
    observation: Mapping[str, Any],
    semantic_judge: Callable[[str, str], tuple[bool, str]] | None = None,
) -> Verdict:
    """按断言种类判定一次观察。"""
    # url / text / semantic 只有一个取值；按「|」拼回，兼容旧版按「|」拆开写出的 values。
    whole = "|".join(assertion.values)
    visible = str(observation.get("text", "") or "")

    if assertion.kind == "url":
        actual = unquote(_path_and_query(observation.get("url", "")))
        return Verdict(passed=normalize(unquote(whole)) in normalize(actual), by="rule", actual=actual)

    if assertion.kind in ALTERNATIVE_KINDS:
        labels = _labels(observation.get("elements", ()))
        matches = [label for label in labels if any(normalize(value) == label for value in assertion.values)]
        if assertion.kind == "element":
            if matches:
                return Verdict(passed=True, by="rule", actual=matches[0])
            return Verdict(
                passed=False,
                by="rule",
                actual=", ".join(labels),
                closest=_closest(assertion.values, labels),
            )
        return Verdict(passed=not matches, by="rule", actual=", ".join(labels))

    if assertion.kind == "text":
        messages = [str(item) for item in observation.get("messages", ()) or ()]
        seen = "\n".join([visible, *messages]) if messages else visible
        return Verdict(passed=normalize(whole) in normalize(seen), by="rule", actual=seen)

    if assertion.kind in ("row", "row_not"):
        rows = table_rows(observation)
        wanted = normalize(whole)
        hits = [row for row in rows if wanted in row]
        actual = "；".join(rows[:ROW_SAMPLE]) if rows else "页面上没有表格数据"
        if assertion.kind == "row":
            return Verdict(passed=bool(hits), by="rule", actual=hits[0] if hits else actual)
        has_table = bool(observation.get("tables"))
        return Verdict(passed=has_table and not hits, by="rule",
                       actual=actual if has_table else "页面上没有表格，无法判定这一行不在")

    if assertion.kind == "semantic":
        if semantic_judge is None:
            raise DslError(f"semantic 断言需要注入 judge：{assertion.source}")
        passed, reason = semantic_judge(whole, visible)
        return Verdict(passed=bool(passed), by="semantic", actual=str(reason))

    raise DslError(f"无法识别的断言写法 {assertion.source!r}：未知种类 {assertion.kind!r}")


def check_all(
    exprs: Sequence[str],
    observation: Mapping[str, Any],
    semantic_judge: Callable[[str, str], tuple[bool, str]] | None = None,
) -> list[Verdict]:
    """按原顺序逐条判定；全部满足才算整步通过。"""
    return [check(parse(expr), observation, semantic_judge) for expr in exprs]


def table_rows(observation: Mapping[str, Any]) -> list[str]:
    """页面上所有表格行，每行各列文字规范化后用空格连起来（按出现顺序）。"""
    rows: list[str] = []
    for table in observation.get("tables", ()) or ():
        if not isinstance(table, Mapping):
            continue
        for row in table.get("rows", ()) or ():
            if isinstance(row, Mapping):
                rows.append(normalize(" ".join(str(value) for value in row.values())))
    return rows


def _path_and_query(url: Any) -> str:
    split = urlsplit(str(url or ""))
    return split.path + (f"?{split.query}" if split.query else "")


def _labels(elements: Any) -> list[str]:
    labels: list[str] = []
    for element in elements or ():
        label = normalize(element.get("label", "") if isinstance(element, Mapping) else "")
        if label and label not in labels:
            labels.append(label)
    return labels


def _closest(values: Sequence[str], labels: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        for candidate in difflib.get_close_matches(normalize(value), labels, n=CLOSEST_LIMIT, cutoff=0.0):
            if candidate not in found:
                found.append(candidate)
    return tuple(found[:CLOSEST_LIMIT])
