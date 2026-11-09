"""把 routes.mjs / vue.mjs 的抽取结果合并成导航图。

对应 docs/implementation.md 第 4.3 节。抽取里不是字面量的值形如 {"dynamic": "<源码片段>"}，
导航图对这类值一律留空，绝不猜测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

DYNAMIC = "dynamic"

DIRECT_VALUES = ("yes", "with_params", "menu", "from_row", "new_tab")
EDGE_KINDS = ("module", "menu", "tab", "click", "url")
EVIDENCE_VALUES = ("archive", "source", "trial")

# 守卫（src/router/guards.ts）会拦截直接访问的路径：登录页、手机端入口、根路径与 403。
GUARD_ENTRY_PATHS = (
    "/",
    "/403",
    "/login",
    "/org-login",
    "/internal-login",
    "/outsource",
    "/feishu-callback",
    "/materialH5",
    "/material-claim",
    "/random-checking-h5",
)

MAX_LANDMARKS = 5


class NavGraphError(ValueError):
    """导航图数据不合法。"""


def is_dynamic(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {DYNAMIC} and isinstance(value[DYNAMIC], str)


def count_dynamic(value: Any) -> int:
    if is_dynamic(value):
        return 1
    if isinstance(value, Mapping):
        return sum(count_dynamic(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(count_dynamic(item) for item in value)
    return 0


def snake_case(text: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced)
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", spaced)
    return re.sub(r"_+", "_", cleaned).strip("_").lower()


def node_id(route_name: Any, path: str) -> str:
    """路由 name 转 snake_case；没有 name 时用完整 path 生成。"""
    name = route_name if isinstance(route_name, str) and route_name.strip() else ""
    ident = snake_case(name) if name else ""
    if not ident:
        ident = snake_case(path)
    if not ident:
        raise NavGraphError(f"无法从 path 生成节点 id：{path!r}")
    return ident


def _check_fields(data: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    if not isinstance(data, Mapping):
        raise NavGraphError(f"{label} 不是对象：{data!r}")
    missing = [name for name in expected if name not in data]
    extra = [name for name in data if name not in expected]
    if missing or extra:
        raise NavGraphError(f"{label} 字段不匹配：缺少 {missing}，多余 {extra}")


def _literal_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class NavNode:
    id: str
    path: str
    route_name: str = ""
    title: str = ""
    backend_key: str = ""
    component: str = ""
    menu: tuple[str, ...] = ()
    required_query: tuple[str, ...] = ()
    direct: str = "yes"
    landmarks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "route_name": self.route_name,
            "title": self.title,
            "backend_key": self.backend_key,
            "component": self.component,
            "menu": list(self.menu),
            "required_query": list(self.required_query),
            "direct": self.direct,
            "landmarks": list(self.landmarks),
        }

    @classmethod
    def from_dict(cls, index: str, data: Mapping[str, Any]) -> NavNode:
        _check_fields(data, ("path", "route_name", "title", "backend_key", "component", "menu", "required_query",
                             "direct", "landmarks"), "NavNode")
        if data["direct"] not in DIRECT_VALUES:
            raise NavGraphError(f"NavNode.direct 不在枚举内：{data['direct']!r}")
        return cls(
            id=index,
            path=data["path"],
            route_name=data["route_name"],
            title=data["title"],
            backend_key=data["backend_key"],
            component=data["component"],
            menu=tuple(data["menu"]),
            required_query=tuple(data["required_query"]),
            direct=data["direct"],
            landmarks=tuple(data["landmarks"]),
        )


@dataclass(frozen=True)
class NavEdge:
    from_: str
    to: str
    kind: str
    label: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.from_, "to": self.to, "kind": self.kind, "label": self.label,
                "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NavEdge:
        _check_fields(data, ("from", "to", "kind", "label", "evidence"), "NavEdge")
        if data["kind"] not in EDGE_KINDS:
            raise NavGraphError(f"NavEdge.kind 不在枚举内：{data['kind']!r}")
        for source in data["evidence"]:
            if source not in EVIDENCE_VALUES:
                raise NavGraphError(f"NavEdge.evidence 不在枚举内：{source!r}")
        return cls(from_=data["from"], to=data["to"], kind=data["kind"], label=data["label"],
                   evidence=tuple(data["evidence"]))


@dataclass(frozen=True)
class NavGraph:
    kb_version: str
    nodes: tuple[NavNode, ...] = ()
    edges: tuple[NavEdge, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kb_version": self.kb_version,
            "nodes": {node.id: node.to_dict() for node in self.nodes},
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NavGraph:
        _check_fields(data, ("kb_version", "nodes", "edges"), "NavGraph")
        nodes = tuple(NavNode.from_dict(index, data["nodes"][index]) for index in sorted(data["nodes"]))
        return cls(
            kb_version=data["kb_version"],
            nodes=nodes,
            edges=tuple(NavEdge.from_dict(edge) for edge in data["edges"]),
        )


def _flatten_routes(routes: Sequence[Any]) -> Iterator[Mapping[str, Any]]:
    for route in routes or ():
        if not isinstance(route, Mapping):
            continue
        yield route
        yield from _flatten_routes(route.get("children") or ())


def _label_index(vue: Mapping[str, Any], bucket: str, field: str) -> dict[str, tuple[str, ...]]:
    index: dict[str, tuple[str, ...]] = {}
    for entry in vue.get("files") or ():
        if not isinstance(entry, Mapping):
            continue
        file = _literal_text(entry.get("file"))
        labels = []
        for item in entry.get(bucket) or ():
            if isinstance(item, Mapping) and isinstance(item.get(field), str):
                labels.append(item[field])
        if file:
            index[file] = tuple(labels)
    return index


def _query_index(vue: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    index: dict[str, tuple[str, ...]] = {}
    for entry in vue.get("files") or ():
        if not isinstance(entry, Mapping):
            continue
        file = _literal_text(entry.get("file"))
        keys = tuple(key for key in entry.get("query_keys") or () if isinstance(key, str))
        if file:
            index[file] = keys
    return index


def _menu_index(modules: Sequence[Any], flat: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    title_by_key: dict[str, str] = {}
    for route in flat:
        meta = route.get("meta") if isinstance(route.get("meta"), Mapping) else {}
        backend_key = _literal_text(meta.get("backendKey"))
        title = _literal_text(meta.get("title"))
        if backend_key and title and backend_key not in title_by_key:
            title_by_key[backend_key] = title

    index: dict[str, tuple[str, ...]] = {}
    for module in modules or ():
        if not isinstance(module, Mapping):
            continue
        module_name = _literal_text(module.get("name"))
        for entry in module.get("entries") or ():
            if not isinstance(entry, Mapping):
                continue
            if entry.get("kind") == "container":
                container_title = (
                    _literal_text(entry.get("title"))
                    or _literal_text(entry.get("name"))
                    or title_by_key.get(_literal_text(entry.get("from_backend_key")), "")
                )
                for leaf in entry.get("leaves") or ():
                    _register_leaf(index, leaf, (module_name, container_title), title_by_key)
            else:
                _register_leaf(index, entry, (module_name,), title_by_key)
    return index


def _register_leaf(
    index: dict[str, tuple[str, ...]],
    leaf: Mapping[str, Any],
    prefix: Sequence[str],
    title_by_key: Mapping[str, str],
) -> None:
    if not isinstance(leaf, Mapping):
        return
    backend_key = _literal_text(leaf.get("backend_key"))
    if not backend_key:
        return
    leaf_title = _literal_text(leaf.get("title")) or title_by_key.get(backend_key, "")
    segments = (*prefix, leaf_title)
    if any(not segment for segment in segments):
        return
    if backend_key in index:
        return
    index[backend_key] = tuple(segments)


def _direct(path: str, required_query: Sequence[str]) -> str:
    # 其余取值（from_row / new_tab）需要试跑证据，留给导航与编译阶段补充。
    if path in GUARD_ENTRY_PATHS:
        return "menu"
    if required_query:
        return "with_params"
    return "yes"


def _landmarks(labels: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"element={label}" for label in labels[:MAX_LANDMARKS])


def _absolute_path(redirect: str, path: str) -> str:
    if redirect.startswith("/"):
        return redirect.rstrip("/") or "/"
    parent = path.rsplit("/", 1)[0]
    return (parent + "/" + redirect).replace("//", "/").rstrip("/") or "/"


def _redirect_edges(flat: Sequence[Mapping[str, Any]], by_path: Mapping[str, NavNode]) -> tuple[NavEdge, ...]:
    edges: dict[tuple[str, str, str, str], NavEdge] = {}
    for route in flat:
        path = route.get("full_path")
        redirect = route.get("redirect")
        if not isinstance(path, str) or not isinstance(redirect, str):
            continue
        source = by_path.get(path)
        target = by_path.get(_absolute_path(redirect, path))
        if source is None or target is None or source.id == target.id:
            continue
        edge = NavEdge(from_=source.id, to=target.id, kind="url", label=target.title, evidence=("source",))
        edges.setdefault((edge.from_, edge.to, edge.kind, edge.label), edge)
    return tuple(edges[key] for key in sorted(edges))


def build_navgraph(routes: Mapping[str, Any], vue: Mapping[str, Any], kb_version: str) -> NavGraph:
    """合并两份抽取结果。nodes 按 id 排序，edges 按 (from, to, kind, label) 排序。"""
    if not isinstance(kb_version, str) or not kb_version.startswith("webui@"):
        raise NavGraphError(f"kb_version 应为 webui@<sha>：{kb_version!r}")
    flat = list(_flatten_routes(routes.get("routes") or ()))
    queries = _query_index(vue)
    buttons = _label_index(vue, "buttons", "text")
    menus = _menu_index(routes.get("modules") or (), flat)

    nodes: list[NavNode] = []
    used: dict[str, int] = {}
    by_path: dict[str, NavNode] = {}
    for route in flat:
        path = route.get("full_path")
        if not isinstance(path, str):
            continue
        meta = route.get("meta") if isinstance(route.get("meta"), Mapping) else {}
        backend_key = _literal_text(meta.get("backendKey"))
        component = _literal_text(route.get("component"))
        ident = node_id(route.get("name"), path)
        if ident in used:
            used[ident] += 1
            ident = f"{ident}-{used[ident]}"
        else:
            used[ident] = 1
        required_query = queries.get(component, ())
        node = NavNode(
            id=ident,
            path=path,
            route_name=_literal_text(route.get("name")),
            title=_literal_text(meta.get("title")),
            backend_key=backend_key,
            component=component,
            menu=menus.get(backend_key, ()),
            required_query=required_query,
            direct=_direct(path, required_query),
            landmarks=_landmarks(buttons.get(component, ())),
        )
        nodes.append(node)
        by_path.setdefault(path, node)

    return NavGraph(
        kb_version=kb_version,
        nodes=tuple(sorted(nodes, key=lambda node: node.id)),
        edges=_redirect_edges(flat, by_path),
    )
