"""串起抽取、导航图与知识卡：知识库构建入口（Step 4）。

用法：python -m jev_convert.kb.build --webui <webui 目录> [--out qa_data/kb]
`jev-convert kb build` 子命令由后续步骤接到 jev_convert/cli.py（本步骤不改 cli.py）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping, Sequence

from jev_convert import jsonio
from jev_convert.kb.cards import build_cards
from jev_convert.kb.navgraph import build_navgraph, count_dynamic

EXTRACT_DIR = Path(__file__).resolve().parent / "extract"
DEFAULT_MODULES_FILE = "src/config/v2Modules.ts"
DEFAULT_MODULES_EXPORT = "V2_MODULES"
DEFAULT_OUT_DIR = Path("qa_data/kb")
MAX_COMPONENTS_PER_CALL = 50
NODE_TIMEOUT_S = 300


class KbError(RuntimeError):
    """知识库构建失败。"""


def git_version(webui_dir: Path) -> str:
    """kb_version 取 webui 的 git 短 SHA（只读命令）。"""
    result = subprocess.run(
        ["git", "-C", str(webui_dir), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise KbError(f"读取 webui 版本失败（git rev-parse）：{result.stderr.strip()}")
    return "webui@" + result.stdout.strip()


def _node_json(script: Path, args: Sequence[str], *, cwd: Path | None, node: str) -> Any:
    if not script.is_file():
        raise KbError(f"找不到抽取脚本：{script}")
    try:
        result = subprocess.run(
            [node, str(script), *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as error:
        raise KbError(f"PATH 里找不到 {node}，无法运行抽取脚本") from error
    except subprocess.TimeoutExpired as error:
        raise KbError(f"抽取脚本超时：{script.name}") from error
    if result.returncode != 0:
        raise KbError(f"{script.name} 失败（退出码 {result.returncode}）：{result.stderr.strip()[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise KbError(f"{script.name} 输出的不是合法 JSON：{error}") from error


def collect_components(routes: Mapping[str, Any]) -> tuple[str, ...]:
    """从嵌套路由里收集组件相对路径，按字典序返回，便于稳定输出。"""
    found: list[str] = []

    def walk(items: Sequence[Any]) -> None:
        for item in items or ():
            if not isinstance(item, Mapping):
                continue
            component = item.get("component")
            if isinstance(component, str) and component.endswith(".vue") and component not in found:
                found.append(component)
            walk(item.get("children") or ())

    walk(routes.get("routes") or ())
    return tuple(sorted(found))


def extract_documents(
    webui_dir: Path,
    *,
    modules_file: str = DEFAULT_MODULES_FILE,
    modules_export: str = DEFAULT_MODULES_EXPORT,
    node: str = "node",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """运行 routes.mjs 与 vue.mjs，返回 (路由抽取, 组件抽取)。"""
    routes = _node_json(
        EXTRACT_DIR / "routes.mjs",
        [str(webui_dir), "--modules", modules_file, "--export", modules_export],
        cwd=None,
        node=node,
    )
    files = tuple(
        component for component in collect_components(routes) if (webui_dir / component).is_file()
    )
    views: list[Mapping[str, Any]] = []
    for start in range(0, len(files), MAX_COMPONENTS_PER_CALL):
        chunk = list(files[start : start + MAX_COMPONENTS_PER_CALL])
        document = _node_json(EXTRACT_DIR / "vue.mjs", chunk, cwd=webui_dir, node=node)
        views.extend(document.get("files") or ())
    views.sort(key=lambda entry: str(entry.get("file", "")))
    return routes, {"files": views}


def write_kb(
    routes: Mapping[str, Any],
    vue: Mapping[str, Any],
    kb_version: str,
    out_dir: Path,
) -> dict[str, Any]:
    """纯函数式的写盘：同一份抽取结果重复运行，qa_data/kb 内容完全一致。"""
    out = Path(out_dir)
    graph = build_navgraph(routes, vue, kb_version)
    cards = build_cards(graph, vue, kb_version)

    jsonio.dump(graph.to_dict(), out / "navgraph.json")
    cards_dir = out / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    expected = {card.filename: card for card in cards}
    for stale in sorted(cards_dir.glob("*.json")):
        if stale.name not in expected:
            stale.unlink()
    for name, card in expected.items():
        jsonio.dump(card.to_dict(), cards_dir / name)

    meta = {
        "kb_version": kb_version,
        "sources": {
            "routes": str(routes.get("routes_file", "")),
            "modules": str(routes.get("modules_file", "")),
        },
        "counts": {
            "nodes": len(graph.nodes),
            "menus": sum(1 for node in graph.nodes if node.menu),
            "edges": len(graph.edges),
            "cards": len(cards),
            "dynamic": count_dynamic(routes) + count_dynamic(vue),
        },
    }
    jsonio.dump(meta, out / "meta.json")
    return meta


def build_kb(
    webui_dir: str | Path,
    out_dir: str | Path = DEFAULT_OUT_DIR,
    *,
    modules_file: str = DEFAULT_MODULES_FILE,
    modules_export: str = DEFAULT_MODULES_EXPORT,
    kb_version: str | None = None,
    node: str = "node",
    documents: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """抽取 + 合并 + 写盘。传入 documents 时跳过 Node 脚本（测试用）。

    progress：阶段回调，阶段开始时收到 "extract" / "write"。
    """
    report = progress if progress is not None else (lambda name: None)
    root = Path(webui_dir).expanduser()
    if not root.is_dir():
        raise KbError(f"webui 目录不存在：{root}")
    report("extract")
    version = kb_version or git_version(root)
    if documents is None:
        routes, vue = extract_documents(root, modules_file=modules_file, modules_export=modules_export, node=node)
    else:
        routes, vue = documents
    report("write")
    return write_kb(routes, vue, version, Path(out_dir))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-convert kb build", description="从 webui 源码生成导航图与页面知识卡")
    parser.add_argument("--webui", required=True, help="webui 源码目录（只读）")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录，默认 qa_data/kb")
    parser.add_argument("--modules", default=DEFAULT_MODULES_FILE, help="菜单配置文件（相对 webui 根）")
    parser.add_argument("--modules-export", default=DEFAULT_MODULES_EXPORT, help="菜单数组的导出名")
    parser.add_argument("--node", default="node", help="node 可执行文件，默认 node")
    parser.add_argument("--version", default=None, help="覆盖 kb_version，默认取 webui 的 git 短 SHA")
    args = parser.parse_args(argv)
    try:
        meta = build_kb(
            args.webui,
            Path(args.out),
            modules_file=args.modules,
            modules_export=args.modules_export,
            kb_version=args.version,
            node=args.node,
        )
    except KbError as error:
        print(f"知识库构建失败：{error}", file=sys.stderr)
        return 1
    counts = meta["counts"]
    print(
        f"kb_version={meta['kb_version']} 节点 {counts['nodes']}，有菜单 {counts['menus']}，"
        f"卡片 {counts['cards']}，边 {counts['edges']}，dynamic {counts['dynamic']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
