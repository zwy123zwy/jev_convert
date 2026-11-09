"""计划库（docs/implementation.md Step 9）：qa_data/plans/<mindnote_token>/<case_id>.json + index.json。

- 写出：同一份计划写两次，文件字节一致（jsonio.dump：键排序、固定缩进）。
- 索引：case_id → 导图节点 id、status、内容哈希、kb_version。
- 增量合并（merge）按“导图原文”判断用例变没变（source_hash：标题、每步的导图节点和原文）：
  - 原文没变、知识库版本也没变 → 保留已有计划（人工确认的结果、学到的定位都留着）；
  - 原文变了，或者知识库版本变了 → 用新编译的，状态按新编译结果（需要重新确认）；
  - 已有计划的导图节点被删掉了 → 标 stale，不删文件；
  - 已有计划的 kb_version 和当前知识库不一致、本次又没有重新编译出它 → 标 stale。
  不比较编译结果：人工在确认表里改过目标、或知识库沉淀了别名以后，编译结果本来就会不同。
- 索引里的 hash 是计划内容哈希（content_hash，不含 status 和学到的 locator），确认表按它去重。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jev_convert import jsonio
from jev_convert.compile.plan import STATUSES, Plan, PlanError

INDEX_FILE = "index.json"
SOURCE_FILE = "source.json"
LABELS_FILE = "labels.json"       # 确认表里标注的写法样例（Step 13），不是计划
SKIP_FILES = (INDEX_FILE, SOURCE_FILE, LABELS_FILE)


def plans_dir(data_dir: Path, mindnote: str) -> Path:
    name = mindnote.replace("/", "_").strip(". ") or "mindnote"
    return Path(data_dir) / "plans" / name


def _strip(item: Plan) -> dict[str, Any]:
    """内容哈希的输入：去掉 status、步骤 status 和学到的定位。"""
    body = item.to_dict()
    body.pop("status", None)
    for key in ("steps", "teardown"):
        for step in body.get(key, ()):
            step.pop("status", None)
            step.get("target", {}).pop("locator", None)
    body["source"].pop("version", None)
    return body


def content_hash(item: Plan) -> str:
    text = json.dumps(_strip(item), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def source_hash(item: Plan) -> str:
    """导图原文的哈希：用例标题 + 每一步的导图节点 id 和原文。"""
    payload = [item.source.node_id, item.title, *[f"{step.source_node}\t{step.original}" for step in item.steps]]
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()[:16]


def _file(item: Plan) -> str:
    name = item.id.replace("/", "_").strip(". ")
    if not name:
        raise PlanError(f"计划 id 不能当文件名：{item.id!r}")
    return f"{name}.json"


def load(directory: Path) -> dict[str, Plan]:
    """读出目录里的全部计划（跳过 index.json / source.json）；坏文件抛 PlanError 并写明文件名。"""
    found: dict[str, Plan] = {}
    folder = Path(directory)
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.json")):
        if path.name in SKIP_FILES:
            continue
        try:
            item = Plan.from_dict(jsonio.load(path))
        except (PlanError, ValueError, KeyError, TypeError) as error:
            raise PlanError(f"{path.name}：{error}") from None
        found[item.id] = item
    return found


def write(directory: Path, items: Iterable[Plan]) -> list[Path]:
    """写出计划和索引。返回写出的计划文件。"""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in sorted(items, key=lambda value: value.id):
        path = folder / _file(item)
        jsonio.dump(item.to_dict(), path)
        written.append(path)
    write_index(folder)
    return written


def write_index(directory: Path) -> dict[str, Any]:
    folder = Path(directory)
    entries = {
        item.id: {"node_id": item.source.node_id, "status": item.status, "hash": content_hash(item),
                  "kb_version": item.kb_version, "file": _file(item)}
        for item in load(folder).values()
    }
    index = {"version": 1, "cases": dict(sorted(entries.items()))}
    jsonio.dump(index, folder / INDEX_FILE)
    return index


@dataclass(frozen=True)
class MergeResult:
    plans: tuple[Plan, ...]
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()


def merge(existing: Mapping[str, Plan], compiled: Sequence[Plan], *, live_nodes: Iterable[str] | None,
          kb_version: str) -> MergeResult:
    """把新编译结果并进已有计划。live_nodes 是导图里现存的节点 id（None 表示不知道，不做删除判断）。"""
    live = set(live_nodes) if live_nodes is not None else None
    merged: dict[str, Plan] = {}
    added: list[str] = []
    changed: list[str] = []
    kept: list[str] = []
    stale: list[str] = []
    for item in compiled:
        old = existing.get(item.id)
        if old is None:
            merged[item.id] = item
            added.append(item.id)
        elif (source_hash(old) == source_hash(item) and old.kb_version == item.kb_version
              and old.status != "stale"):
            merged[item.id] = old
            kept.append(item.id)
        else:
            merged[item.id] = item
            changed.append(item.id)
    for case_id, old in existing.items():
        if case_id in merged:
            continue
        gone = live is not None and old.source.node_id not in live
        outdated = old.kb_version != kb_version
        if (gone or outdated) and old.status != "stale":
            merged[case_id] = with_status(old, "stale")
            stale.append(case_id)
        else:
            merged[case_id] = old
    return MergeResult(plans=tuple(merged[key] for key in sorted(merged)), added=tuple(added),
                       changed=tuple(changed), kept=tuple(kept), stale=tuple(stale))


def with_status(item: Plan, status: str) -> Plan:
    """用例和它的全部步骤用同一个状态（与 s4_plan.with_status 相同，放在这里避免循环依赖）。"""
    if status not in STATUSES:
        raise PlanError(f"未知状态 {status!r}")
    return replace(
        item,
        status=status,
        steps=tuple(replace(step, status=status) for step in item.steps),
        teardown=tuple(replace(step, status=status) for step in item.teardown),
    )


def _live_nodes(directory: Path) -> set[str] | None:
    """S0 写出的 source.json 里现存的导图节点 id；没有 source.json 时返回 None（不做删除判断）。"""
    path = Path(directory) / SOURCE_FILE
    if not path.exists():
        return None
    try:
        data = jsonio.load(path)
    except (OSError, ValueError):
        return None
    return {str(node.get("id")) for node in data.get("nodes", ()) if isinstance(node, Mapping)}


def save(data_dir: Path, compiled: Sequence[Plan], *, kb_version: str | None) -> dict[str, MergeResult]:
    """按导图分目录，把新编译出的计划并进计划库并写盘。返回每份导图的合并结果。

    kb_version 为 None（知识库不在）时不按知识库版本判过期。
    """
    groups: dict[str, list[Plan]] = {}
    for item in compiled:
        groups.setdefault(item.source.mindnote, []).append(item)
    results: dict[str, MergeResult] = {}
    for mindnote, items in sorted(groups.items()):
        folder = plans_dir(data_dir, mindnote)
        existing = load(folder)
        version = kb_version if kb_version is not None else (items[0].kb_version if items else "")
        result = merge(existing, items, live_nodes=_live_nodes(folder), kb_version=version)
        write(folder, result.plans)
        results[mindnote] = result
    return results
