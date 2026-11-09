"""从 qa_data/kb 读取导航图与页面知识卡。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from jev_convert.kb.cards import PageCard
from jev_convert.kb.navgraph import NavGraph


class KbLoadError(RuntimeError):
    """知识库文件缺失或损坏。"""


def load_graph(kb_dir: Path) -> NavGraph:
    path = Path(kb_dir) / "navgraph.json"
    if not path.exists():
        raise KbLoadError(f"找不到 {path}：先运行 jev-convert kb build")
    return NavGraph.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_cards(kb_dir: Path) -> dict[str, PageCard]:
    directory = Path(kb_dir) / "cards"
    if not directory.is_dir():
        raise KbLoadError(f"找不到知识卡目录 {directory}：先运行 jev-convert kb build")
    cards: dict[str, PageCard] = {}
    for path in sorted(directory.glob("*.json")):
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        card = PageCard.from_dict(data)
        cards[card.node] = card
    return apply_confirmed(cards, load_confirmed(kb_dir))


CONFIRMED_FILE = "confirmed.json"


def load_confirmed(kb_dir: Path) -> dict[str, dict[str, Any]]:
    """确认表沉淀下来的知识（Step 13）：节点 → {"aliases": {说法: 元素名}, "verified": {元素名: 次数}}。"""
    path = Path(kb_dir) / CONFIRMED_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = data.get("pages", {}) if isinstance(data, dict) else {}
    return {str(node): dict(item) for node, item in pages.items() if isinstance(item, dict)}


def apply_confirmed(cards: dict[str, PageCard], confirmed: dict[str, dict[str, Any]]) -> dict[str, PageCard]:
    """叠加到知识卡上：确认过的元素记为 source=archive、verified 加上确认次数；别名并入 aliases。

    知识卡文件本身不改：它由 kb build 生成，写回会被下次构建覆盖。
    """
    for node, item in confirmed.items():
        card = cards.get(node)
        if card is None:
            continue
        verified = {str(key): int(value) for key, value in (item.get("verified") or {}).items()}
        elements = tuple(
            replace(element, source="archive", verified=element.verified + verified[element.label])
            if isinstance(element.label, str) and element.label in verified else element
            for element in card.elements
        )
        aliases = {**card.aliases, **{str(key): str(value) for key, value in (item.get("aliases") or {}).items()}}
        cards[node] = replace(card, elements=elements, aliases=aliases)
    return cards
