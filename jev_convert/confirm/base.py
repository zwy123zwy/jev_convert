"""人工确认（docs/implementation.md Step 13）：draft 计划里需要人判断的步骤写进飞书多维表格，读回后更新计划。

表的字段（字段名固定，表需要事先建好，见 FIELDS；jev-convert 不自动建表）：

| 字段 | 类型 | 谁写 | 说明 |
| 键 | 文本 | jev-convert | <用例 id>#<步骤序号>@<计划内容哈希>：同一份计划内容只写一次 |
| 用例 | 文本 | jev-convert | 用例 id 和标题 |
| 步骤 | 文本 | jev-convert | 导图原文 |
| 页面 | 文本 | jev-convert | 落地的页面节点 |
| 当前目标 | 文本 | jev-convert | 编译选出的元素（可能为空） |
| 候选 | 文本 | jev-convert | 知识卡里的候选元素，每行“编号. 名称” |
| 问题 | 文本 | jev-convert | 为什么要人确认（置信度低、静态校验的问题） |
| 结论 | 单选 | 人 | 采纳 / 改为候选 / 手填 / 否决 |
| 候选编号 | 数字 | 人 | 结论为“改为候选”时填 |
| 手填目标 | 文本 | 人 | 结论为“手填”时填真实元素名 |
| 备注 | 文本 | 人 | 可选 |
| 状态 | 单选 | jev-convert | 待确认 / 已处理 / 已过期 / 有问题 |
| 处理说明 | 文本 | jev-convert | 读回时的处理结果 |

读回（apply）：
- 采纳：保持当前目标；改为候选：换成第 N 个候选；手填：换成手填的元素名；否决：计划标 rejected。
- 计划内容哈希和表里的不一致（计划在写表之后又被重新编译了）→ 状态记“已过期”，不改计划。
- 改完的计划重新过 S6；一条用例所有需要确认的步骤都处理完、没有问题时标 confirmed，否则留 draft。
- 确认过的（说法 → 元素）写进 qa_data/kb/confirmed.json：下次编译 S3 直接用这个别名，元素记为已归档。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from jev_convert import jsonio
from jev_convert.compile import library, s6_check
from jev_convert.compile.plan import Plan, Step
from jev_convert.compile.s3_ground import phrases
from jev_convert.kb.cards import PageCard
from jev_convert.kb.load import CONFIRMED_FILE
from jev_convert.kb.navgraph import NavGraph
from jev_convert.lark import Lark

FIELDS = ("键", "用例", "步骤", "页面", "当前目标", "候选", "问题", "结论", "候选编号", "手填目标", "备注", "状态",
          "处理说明")
ACCEPT, PICK, MANUAL, REJECT = "采纳", "改为候选", "手填", "否决"
PENDING, DONE, OUTDATED, INVALID = "待确认", "已处理", "已过期", "有问题"
MAX_CANDIDATES = 8
ROLE_NAMES = {"module": "模块", "feature": "功能点", "case": "用例", "step": "步骤", "expect": "预期",
              "precondition": "前置条件"}
LABEL_PREFIX = "标注"
SAMPLE_LIMIT = 12


class ConfirmError(ValueError):
    """确认表没配置或表结构不对。"""


@dataclass(frozen=True)
class Item:
    """确认表里的一行（只含 jev-convert 写的字段）。"""

    key: str
    case_id: str
    seq: int
    content_hash: str
    fields: Mapping[str, Any]


def _key(item: Plan, seq: int) -> str:
    return f"{item.id}#{seq}@{library.content_hash(item)}"


def _parse_key(key: str) -> tuple[str, int, str] | None:
    head, sep, digest = str(key).rpartition("@")
    case_id, sep2, seq = head.rpartition("#")
    if not sep or not sep2 or not seq.isdigit():
        return None
    return case_id, int(seq), digest


def _candidates(card: PageCard | None, step: Step) -> list[str]:
    if card is None:
        return []
    labels = [element.label for element in card.elements if isinstance(element.label, str) and element.label]
    labels = list(dict.fromkeys(labels))
    current = step.target.name
    labels.sort(key=lambda label: (label != current, label))
    return labels[:MAX_CANDIDATES]


def items_for(plans: Sequence[Plan], graph: NavGraph, cards: Mapping[str, PageCard], threshold: float) -> list[Item]:
    """draft 计划里需要人判断的步骤：置信度低于阈值、目标为空，或 S6 对这一步报了问题。"""
    found: list[Item] = []
    for item in plans:
        if item.status != "draft":
            continue
        problems = s6_check.check_plan(item, graph, cards)
        by_step: dict[int, list[str]] = {}
        for problem in problems:
            where = problem.where
            if where.startswith("steps["):
                seq = int(where[len("steps["):where.index("]")])
                by_step.setdefault(seq, []).append(problem.message)
            else:
                by_step.setdefault(1, []).append(problem.message)
        for step in item.steps:
            reasons = list(by_step.get(step.seq, []))
            if step.kind not in ("navigate", "wait") and (not step.target.name or step.target.confidence < threshold):
                reasons.insert(0, f"目标置信度 {step.target.confidence:.2f} 低于 {threshold:.2f}")
            if not reasons:
                continue
            options = _candidates(cards.get(step.page), step)
            fields = {
                "键": _key(item, step.seq),
                "用例": f"{item.id} {item.title}",
                "步骤": step.original,
                "页面": step.page,
                "当前目标": step.target.name,
                "候选": "\n".join(f"{index}. {label}" for index, label in enumerate(options, start=1)),
                "问题": "\n".join(dict.fromkeys(reasons)),
                "状态": PENDING,
            }
            found.append(Item(key=fields["键"], case_id=item.id, seq=step.seq,
                              content_hash=library.content_hash(item), fields=fields))
    return found


def _cells(fields: Mapping[str, Any]) -> dict[str, Any]:
    """写表前的格式：单选字段（状态）写成选项名数组，其他文本原样。"""
    cells = dict(fields)
    if isinstance(cells.get("状态"), str):
        cells["状态"] = [cells["状态"]]
    return cells


def push(lark: Lark, base_token: str, table_id: str, items: Sequence[Item]) -> int:
    """只写表里还没有的键。返回新写入的行数。"""
    existing = {str(_text(row["fields"].get("键"))) for row in lark.base_records(base_token, table_id)}
    rows = [_cells(item.fields) for item in items if item.key not in existing]
    if rows:
        lark.base_create(base_token, table_id, rows)
    return len(rows)


def _text(value: Any) -> str:
    """单元格的文字：文本直接用；单选 / 多选取第一个；数字转成字符串；富文本数组拼起来。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return value[0].strip() if value else ""
        return "".join(str(item.get("text", "")) for item in value if isinstance(item, Mapping)).strip()
    if isinstance(value, Mapping):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


@dataclass
class ApplyResult:
    plans: dict[str, Plan] = field(default_factory=dict)        # 改过的计划
    updates: dict[str, dict[str, Any]] = field(default_factory=dict)   # record_id → 回写到表的字段
    confirmed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    aliases: list[tuple[str, str, str]] = field(default_factory=list)  # (页面, 说法, 元素名)


def _decide(step: Step, fields: Mapping[str, Any]) -> tuple[str | None, str]:
    """返回（新的目标名，说明）；目标名为 None 表示结论无效或尚未填写。"""
    verdict = _text(fields.get("结论"))
    if verdict == ACCEPT:
        if not step.target.name:
            return None, "当前目标为空，不能采纳：请改为候选或手填"
        return step.target.name, "采纳当前目标"
    if verdict == PICK:
        number = _text(fields.get("候选编号"))
        options = [line.split(". ", 1)[1] for line in _text(fields.get("候选")).splitlines() if ". " in line]
        if not number.isdigit() or not 1 <= int(number) <= len(options):
            return None, f"候选编号 {number or '（空）'} 不在 1–{len(options)} 之间"
        return options[int(number) - 1], f"改为候选 {number}"
    if verdict == MANUAL:
        name = _text(fields.get("手填目标"))
        return (name, "手填目标") if name else (None, "结论是手填，但手填目标为空")
    return None, ""


def apply(rows: Sequence[Mapping[str, Any]], plans: Mapping[str, Plan], graph: NavGraph,
          cards: Mapping[str, PageCard], threshold: float) -> ApplyResult:
    """按表里的结论更新计划。只处理“待确认”且填了结论的行。

    一条用例的每个动作步骤都有目标、置信度达到 threshold（人工确认过的记为 1.0），并且 S6 没有问题，
    才标 confirmed；还有没处理的步骤就留 draft。
    """
    result = ApplyResult()
    working = dict(plans)
    touched: set[str] = set()
    for row in rows:
        fields = row.get("fields", {})
        if _text(fields.get("状态")) not in ("", PENDING) or not _text(fields.get("结论")):
            continue
        key = _text(fields.get("键"))
        if key.startswith(f"{LABEL_PREFIX}#"):
            continue            # 写法样例标注，由 read_labels 处理
        parsed = _parse_key(key)
        record_id = str(row.get("record_id", ""))
        if parsed is None:
            result.updates[record_id] = {"状态": INVALID, "处理说明": "键的格式不对"}
            continue
        case_id, seq, digest = parsed
        if case_id not in plans:
            continue            # 别的导图的用例：多张导图可以共用一张确认表
        item = working.get(case_id)
        original = plans.get(case_id)
        if item is None or original is None or library.content_hash(original) != digest:
            result.updates[record_id] = {"状态": OUTDATED,
                                         "处理说明": "计划在写表之后变了，这一行作废；重新导出后再确认"}
            continue
        if _text(fields.get("结论")) == REJECT:
            working[case_id] = library.with_status(item, "rejected")
            touched.add(case_id)
            result.rejected.append(case_id)
            result.updates[record_id] = {"状态": DONE, "处理说明": "用例已否决"}
            continue
        step = next((value for value in item.steps if value.seq == seq), None)
        if step is None:
            result.updates[record_id] = {"状态": INVALID, "处理说明": f"计划里没有第 {seq} 步"}
            continue
        name, note = _decide(step, fields)
        if name is None:
            result.updates[record_id] = {"状态": INVALID, "处理说明": note or "结论无效"}
            continue
        evidence = tuple(dict.fromkeys((*step.target.evidence, "archive")))
        target = replace(step.target, name=name, confidence=1.0, evidence=evidence, locator="")
        goal = step.goal.replace(step.target.name, name) if step.target.name else step.goal
        working[case_id] = replace(item, steps=tuple(replace(value, target=target, goal=goal)
                                                     if value.seq == seq else value for value in item.steps))
        touched.add(case_id)
        result.updates[record_id] = {"状态": DONE, "处理说明": f"{note}：{name}"}
        for phrase in phrases(step.original):
            if phrase != name:
                result.aliases.append((step.page, phrase, name))
    for case_id in sorted(touched):
        item = working[case_id]
        if item.status == "rejected":
            result.plans[case_id] = item
            continue
        problems = s6_check.check_plan(item, graph, cards)
        pending = [step for step in item.steps if step.kind not in ("navigate", "wait")
                   and (not step.target.name or step.target.confidence < threshold)]
        status = "confirmed" if not problems and not pending and item.automatable else "draft"
        result.plans[case_id] = library.with_status(item, status)
        if status == "confirmed":
            result.confirmed.append(case_id)
    return result


def record_knowledge(kb_dir: Path, aliases: Sequence[tuple[str, str, str]]) -> dict[str, Any]:
    """把确认过的说法 → 元素写进 confirmed.json（累加 verified 次数）。返回写出的内容。"""
    path = Path(kb_dir) / CONFIRMED_FILE
    data: dict[str, Any] = jsonio.load(path) if path.exists() else {"version": 1, "pages": {}}
    pages = data.setdefault("pages", {})
    for page, phrase, name in aliases:
        entry = pages.setdefault(page, {"aliases": {}, "verified": {}})
        entry.setdefault("aliases", {})[phrase] = name
        verified = entry.setdefault("verified", {})
        verified[name] = int(verified.get(name, 0)) + 1
    data["pages"] = {node: pages[node] for node in sorted(pages)}
    if aliases:
        jsonio.dump(data, path)
    return data


def describe_fields() -> str:
    return "、".join(FIELDS)


# ---------- 写法样例标注（画像吻合度低时，Step 10 / 13） ----------



def label_items(mindnote: str, nodes: Sequence[Any], score: float, threshold: float) -> list[Item]:
    """吻合度低：取前 SAMPLE_LIMIT 个非根节点，请人标角色。键是“标注#<导图>#<节点 id>”，不随计划变化。"""
    options = "\n".join(f"{index}. {name}" for index, name in enumerate(ROLE_NAMES.values(), start=1))
    found: list[Item] = []
    for node in [item for item in nodes if item.parent][:SAMPLE_LIMIT]:
        key = f"{LABEL_PREFIX}#{mindnote}#{node.id}"
        fields = {"键": key, "用例": f"写法样例（导图 {mindnote}）", "步骤": node.text, "页面": "", "当前目标": "",
                  "候选": options, "问题": f"写法画像吻合度 {score:.2f} 低于 {threshold:.2f}：请标出这个节点是什么",
                  "状态": PENDING}
        found.append(Item(key=key, case_id="", seq=0, content_hash="", fields=fields))
    return found


def read_labels(rows: Sequence[Mapping[str, Any]], mindnote: str) -> tuple[dict[str, str], dict[str, dict]]:
    """读回标注：返回（节点 id → 角色，record_id → 回写字段）。结论写“改为候选”加编号。"""
    roles = list(ROLE_NAMES)
    labels: dict[str, str] = {}
    updates: dict[str, dict] = {}
    prefix = f"{LABEL_PREFIX}#{mindnote}#"
    for row in rows:
        fields = row.get("fields", {})
        key = _text(fields.get("键"))
        if not key.startswith(prefix) or _text(fields.get("状态")) not in ("", PENDING):
            continue
        if not _text(fields.get("结论")):
            continue
        number = _text(fields.get("候选编号"))
        record_id = str(row.get("record_id", ""))
        if _text(fields.get("结论")) != PICK or not number.isdigit() or not 1 <= int(number) <= len(roles):
            updates[record_id] = {"状态": INVALID, "处理说明": f"标注请选“{PICK}”并填 1–{len(roles)} 的编号"}
            continue
        role = roles[int(number) - 1]
        labels[key[len(prefix):]] = role
        updates[record_id] = {"状态": DONE, "处理说明": f"标为{ROLE_NAMES[role]}"}
    return labels, updates


# ---------- 编排：compile 之后导出，confirm 读回 ----------


def table_of(lark_config: Any) -> tuple[str, str] | None:
    base, table = lark_config.confirm_base_token, lark_config.confirm_table_id
    return (base, table) if base and table else None


def export(lark: Lark, table: tuple[str, str], items: Sequence[Item]) -> int:
    return push(lark, table[0], table[1], items)


@dataclass
class Collected:
    applied: ApplyResult
    labels: dict[str, str]
    rows_updated: int


def collect(lark: Lark, table: tuple[str, str], data_dir: Path, mindnote: str, graph: NavGraph,
            cards: Mapping[str, PageCard], threshold: float,
            progress: Callable[[str], None] | None = None) -> Collected:
    """读回确认表：更新这份导图的计划和写法标注，把确认结果写进知识库，再把处理结果写回表。

    progress：阶段回调，阶段开始时收到 "read" / "apply" / "settle"。
    """
    report = progress if progress is not None else (lambda name: None)
    report("read")
    rows = lark.base_records(table[0], table[1])
    folder = library.plans_dir(data_dir, mindnote)
    labels, label_updates = read_labels(rows, mindnote)
    if labels:
        path = folder / library.LABELS_FILE
        old = jsonio.load(path).get("labels", {}) if path.exists() else {}
        jsonio.dump({"version": 1, "labels": dict(sorted({**old, **labels}.items()))}, path)
    report("apply")
    applied = apply(rows, library.load(folder), graph, cards, threshold)
    if applied.plans:
        library.write(folder, applied.plans.values())
    report("settle")
    record_knowledge(Path(data_dir) / "kb", applied.aliases)
    updates = {record_id: _cells(fields) for record_id, fields in {**label_updates, **applied.updates}.items()
               if record_id}
    if updates:
        lark.base_update(table[0], table[1], updates)
    return Collected(applied=applied, labels=labels, rows_updated=len(updates))
