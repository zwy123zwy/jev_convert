"""S0：读取飞书思维笔记（mindnote）的节点树，规范化后落到 <data_dir>/plans/<token>/source.json。

read() 只做「链接 → 节点树」：wiki 链接解包成 mindnote token，拉节点树，规范化成
{id, parent, order, text} 的扁平列表（深度优先，兄弟按 order），并记录导图 version。
缺权限时区分两种情况：MissingScope 转成带授权链接的 AuthRequired（用户授权后重跑同一条命令），
NoPermission 原样抛出让调用方提示去找导图所有者申请。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from jev_convert import jsonio
from jev_convert.lark import Lark, LarkError, MissingScope

NESTED_KEYS = ("children", "nodes", "items", "elements")
ID_KEYS = ("node_id", "id", "token", "uuid")
PARENT_KEYS = ("parent_id", "parent", "pid")
ORDER_KEYS = ("order", "index", "seq", "sort", "position")
TEXT_KEYS = ("text", "title", "content", "name")
TEXT_INNER_KEYS = ("text", "content", "title", "name", "value")
TOKEN_KEYS = ("obj_token", "document_id", "token")
VERSION_KEYS = ("version", "obj_version", "revision", "obj_edit_time", "edit_time")
AUTH_KEYS = ("url", "verification_url", "authorization_url", "auth_url", "login_url", "verification_uri")
MINDNOTE_TYPES = frozenset({"mindnote", "mind_note", "mindnotes", "mindnote_document"})
CONTENT_VERSION_LENGTH = 16


class AuthRequired(Exception):
    """需要用户先授权：url 是 lark-cli 给出的授权链接，授权后重新运行同一条命令即可。"""

    def __init__(self, url: str, message: str = "") -> None:
        self._url = str(url or "")
        super().__init__(message or "飞书授权不足：请打开授权链接授权后重新运行同一条命令")

    @property
    def url(self) -> str:
        return self._url


class _Missing:
    """区分「字段不存在」与「字段值为 None」。"""


_MISSING = _Missing()


def _clean(text: str) -> str:
    return " ".join(str(text).split())


def _as_text(value: Any) -> str | None:
    """节点文字可能是字符串、富文本片段列表，或带 text 字段的对象。"""
    if isinstance(value, str):
        return _clean(value) or None
    if isinstance(value, Mapping):
        for key in TEXT_INNER_KEYS:
            inner = _as_text(value.get(key))
            if inner:
                return inner
        return None
    if isinstance(value, (list, tuple)):
        parts = [_as_text(item) for item in value]
        joined = "".join(part for part in parts if part)
        return joined or None
    return None


def _first_text(data: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        text = _as_text(data.get(key))
        if text:
            return text
    return None


def _text_of(item: Mapping[str, Any]) -> str:
    return _first_text(item, TEXT_KEYS) or ""


def _order_of(item: Mapping[str, Any], position: int) -> int:
    for key in ORDER_KEYS:
        value = item.get(key, _MISSING)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return position


def _nested(item: Mapping[str, Any]) -> Sequence[Any]:
    for key in NESTED_KEYS:
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            return value
    return ()


def _check_mapping(data: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{where} 必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


def _check_fields(data: Mapping[str, Any], where: str, required: Sequence[str]) -> None:
    unknown = sorted(set(data) - set(required))
    if unknown:
        raise ValueError(f"{where} 含未知字段：{', '.join(unknown)}")
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"{where} 缺少必填字段：{', '.join(missing)}")


def _check_text(data: Mapping[str, Any], where: str, key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} 必须是字符串，实际是 {type(value).__name__}")
    return value


def _check_int(data: Mapping[str, Any], where: str, key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}.{key} 必须是整数，实际是 {type(value).__name__}")
    return int(value)


@dataclass(frozen=True)
class MindNode:
    id: str
    parent: str | None
    order: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "parent": self.parent, "order": self.order, "text": self.text}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MindNode:
        where = "node"
        body = _check_mapping(data, where)
        _check_fields(body, where, ("id", "parent", "order", "text"))
        parent = body["parent"]
        if parent is not None and not isinstance(parent, str):
            raise ValueError(f"{where}.parent 必须是字符串或 null，实际是 {type(parent).__name__}")
        return cls(
            id=_check_text(body, where, "id"),
            parent=parent,
            order=_check_int(body, where, "order"),
            text=_check_text(body, where, "text"),
        )


@dataclass(frozen=True)
class Mindnote:
    token: str
    version: str
    nodes: tuple[MindNode, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "version": self.version,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Mindnote:
        where = "mindnote"
        body = _check_mapping(data, where)
        _check_fields(body, where, ("token", "version", "nodes"))
        nodes = body["nodes"]
        if not isinstance(nodes, (list, tuple)):
            raise ValueError(f"{where}.nodes 必须是数组，实际是 {type(nodes).__name__}")
        return cls(
            token=_check_text(body, where, "token"),
            version=_check_text(body, where, "version"),
            nodes=tuple(MindNode.from_dict(item) for item in nodes),
        )


def _normalize(raw: Sequence[Any]) -> tuple[MindNode, ...]:
    """把 lark-cli 返回的节点树（嵌套 children 或扁平 parent_id 均可）规范成深度优先的节点列表。"""
    found: list[dict[str, Any]] = []

    def walk(items: Sequence[Any], enclosing: str | None) -> None:
        for position, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            node_id = _first_text(item, ID_KEYS) or f"node-{len(found) + 1}"
            found.append(
                {
                    "id": node_id,
                    "declared": _first_text(item, PARENT_KEYS),
                    "enclosing": enclosing,
                    "order": _order_of(item, position),
                    "text": _text_of(item),
                    "seq": len(found),
                }
            )
            walk(_nested(item), node_id)

    walk(tuple(raw), None)
    known = {entry["id"] for entry in found}
    parent: dict[str, str | None] = {}
    for entry in found:
        candidates = (entry["declared"], entry["enclosing"])
        parent[entry["id"]] = next(
            (str(item) for item in candidates if item and item in known and item != entry["id"]), None
        )

    children: dict[str | None, list[dict[str, Any]]] = {}
    for entry in found:
        children.setdefault(parent[entry["id"]], []).append(entry)
    for siblings in children.values():
        siblings.sort(key=lambda entry: (entry["order"], entry["seq"]))

    nodes: list[MindNode] = []
    seen: set[str] = set()

    def emit(entry: Mapping[str, Any]) -> None:
        if entry["id"] in seen:
            return
        seen.add(entry["id"])
        nodes.append(
            MindNode(id=entry["id"], parent=parent[entry["id"]], order=entry["order"], text=entry["text"])
        )
        for child in children.get(entry["id"], ()):
            emit(child)

    for entry in children.get(None, ()):
        emit(entry)
    for entry in found:  # 父链成环等异常：按原始顺序补回，父记为 None
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        nodes.append(MindNode(id=entry["id"], parent=None, order=entry["order"], text=entry["text"]))
    return tuple(nodes)


def _content_version(nodes: Sequence[MindNode]) -> str:
    """导图没有给 version 时，用节点内容的哈希当版本，保证同一份内容得到同一个值。"""
    payload = "\n".join(f"{node.id}\t{node.parent}\t{node.order}\t{node.text}" for node in nodes)
    return sha256(payload.encode("utf-8")).hexdigest()[:CONTENT_VERSION_LENGTH]


def _auth_link(lark: Lark, scopes: Sequence[str]) -> str:
    """向 lark-cli 要授权链接：lark-cli auth login --scope <缺的权限> --no-wait。"""
    args = ["auth", "login", "--no-wait"]
    if scopes:
        args += ["--scope", " ".join(scopes)]
    try:
        envelope = lark.run(args)
    except LarkError:
        return ""
    data = envelope.get("data") if isinstance(envelope, Mapping) else None
    if not isinstance(data, Mapping):
        return ""
    return _first_text(data, AUTH_KEYS) or ""


def _auth_message(error: MissingScope) -> str:
    scopes = "、".join(error.scopes) or "未知"
    return f"飞书授权不足（缺少权限范围：{scopes}）：请打开授权链接授权后重新运行同一条命令"


def _source_path(token: str, data_dir: Path) -> Path:
    name = str(token)
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"mindnote token 不能用作目录名：{name!r}")
    return Path(data_dir) / "plans" / name / "source.json"


def read(url: str, lark: Lark, data_dir: Path) -> Mindnote:
    """读取导图节点树并写出 source.json；缺权限抛 AuthRequired / NoPermission。"""
    try:
        info = lark.resolve_wiki(url)
        token = _first_text(info, TOKEN_KEYS)
        kind = str(info.get("obj_type") or info.get("type") or "").strip().lower()
        if kind and kind not in MINDNOTE_TYPES:
            raise LarkError(f"链接 {url} 指向的文档类型是 {kind}，不是思维笔记（mindnote）")
        if not token:
            raise LarkError(f"没能从 {url} 解析出 mindnote token：{sorted(info)}")
        raw = lark.mindnote_nodes(token)
    except MissingScope as error:
        raise AuthRequired(_auth_link(lark, error.scopes), _auth_message(error)) from error

    nodes = _normalize(raw)
    version = _first_text(info, VERSION_KEYS) or _content_version(nodes)
    mindnote = Mindnote(token=token, version=version, nodes=nodes)
    jsonio.dump(mindnote.to_dict(), _source_path(token, data_dir))
    return mindnote


def resolve_token(url: str, lark: Lark) -> str:
    """只解析链接得到 mindnote token，不拉节点（confirm 用）；缺权限抛 AuthRequired。"""
    try:
        info = lark.resolve_wiki(url)
    except MissingScope as error:
        raise AuthRequired(_auth_link(lark, error.scopes), _auth_message(error)) from error
    token = _first_text(info, TOKEN_KEYS)
    if not token:
        raise LarkError(f"没能从 {url} 解析出 mindnote token：{sorted(info)}")
    return token


def auth_required(lark: Lark, error: MissingScope) -> AuthRequired:
    """缺权限时给调用方的异常：带授权链接。只有真的缺 scope 才生成链接。"""
    return AuthRequired(_auth_link(lark, error.scopes), _auth_message(error))


def load(token: str, data_dir: Path) -> Mindnote:
    """读回上一次 read() 写出的节点树。"""
    return Mindnote.from_dict(jsonio.load(_source_path(token, data_dir)))


def diff(old: Mindnote, new: Mindnote) -> dict[str, tuple[str, ...]]:
    """按 node id 比较两版节点树：新增 / 修改（文字或父节点变了）/ 删除。

    只调了顺序不算修改：在前面插入一个兄弟节点时，后面的兄弟 order 都会变，但内容没变。
    """
    before = {node.id: node for node in old.nodes}
    after = {node.id: node for node in new.nodes}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        node_id
        for node_id in set(before) & set(after)
        if (before[node_id].text, before[node_id].parent) != (after[node_id].text, after[node_id].parent)
    )
    return {"added": tuple(added), "changed": tuple(changed), "removed": tuple(removed)}
