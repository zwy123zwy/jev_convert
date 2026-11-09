import json
from pathlib import Path
from typing import Any

import pytest

from jev_convert.compile.s0_read import AuthRequired, MindNode, Mindnote, diff, load, read
from jev_convert.jsonio import dumps
from jev_convert.jsonio import load as json_load
from jev_convert.lark import LarkError, MissingScope, NoPermission

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mindnotes"


def payload(name: str = "expect_as_child.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_lark(
    data: dict[str, Any] | None = None,
    *,
    info: dict[str, Any] | None = None,
    error: Exception | None = None,
    error_at: str = "resolve",
    auth: dict[str, Any] | None = None,
) -> "FakeLark":
    body = payload() if data is None else data
    default_info = {"obj_token": body["token"], "obj_type": "mindnote", "version": body["version"]}
    return FakeLark(
        body,
        info=default_info if info is None else info,
        error=error,
        error_at=error_at,
        auth={"ok": True, "data": {"url": "https://open.feishu.cn/auth/abc"}} if auth is None else auth,
    )


class FakeLark:
    """假 Lark：预置 resolve_wiki / mindnote_nodes 的返回；run() 只用来取授权链接。"""

    def __init__(
        self,
        data: dict[str, Any],
        *,
        info: dict[str, Any],
        error: Exception | None,
        error_at: str,
        auth: dict[str, Any],
    ) -> None:
        self.data = data
        self.info = info
        self.error = error
        self.error_at = error_at
        self.auth = auth
        self.calls: list[tuple[Any, ...]] = []

    def resolve_wiki(self, url: str) -> dict[str, Any]:
        self.calls.append(("resolve_wiki", url))
        if self.error is not None and self.error_at == "resolve":
            raise self.error
        return dict(self.info)

    def mindnote_nodes(self, token: str) -> list[dict[str, Any]]:
        self.calls.append(("mindnote_nodes", token))
        if self.error is not None and self.error_at == "nodes":
            raise self.error
        return [dict(node) for node in self.data["nodes"]]

    def run(self, args: list[str]) -> dict[str, Any]:
        self.calls.append(("run", tuple(args)))
        return dict(self.auth)


def test_nested_tree_is_flattened_depth_first_with_orders(tmp_path: Path) -> None:
    mindnote = read("https://x.feishu.cn/wiki/wik_1", make_lark(), tmp_path)

    assert mindnote.token == "mnExpectChild"
    assert [node.id for node in mindnote.nodes] == [
        "n_1",
        "n_2",
        "n_3",
        "n_10",
        "n_11",
        "n_12",
        "n_13",
        "n_14",
        "n_15",
    ]
    parents = {node.id: node.parent for node in mindnote.nodes}
    assert parents["n_1"] is None
    assert parents["n_10"] == "n_3"
    assert parents["n_13"] == "n_12"
    orders = {node.id: node.order for node in mindnote.nodes}
    assert orders["n_11"] == 0
    assert orders["n_12"] == 1
    texts = {node.id: node.text for node in mindnote.nodes}
    assert texts["n_10"] == "处理一条任务"
    assert texts["n_11"] == "前置条件：已用测试账号登录"


def test_read_writes_source_json_and_load_reads_it_back(tmp_path: Path) -> None:
    lark = make_lark()
    mindnote = read("https://x.feishu.cn/wiki/wik_1", lark, tmp_path)

    path = tmp_path / "plans" / "mnExpectChild" / "source.json"
    assert path.exists()
    assert json_load(path) == mindnote.to_dict()
    assert load("mnExpectChild", tmp_path) == mindnote
    assert mindnote.version == "1792028614"


def test_read_is_deterministic(tmp_path: Path) -> None:
    first = read("https://x.feishu.cn/wiki/wik_1", make_lark(), tmp_path / "a")
    second = read("https://x.feishu.cn/wiki/wik_1", make_lark(), tmp_path / "b")

    assert first == second
    left = (tmp_path / "a" / "plans" / "mnExpectChild" / "source.json").read_text(encoding="utf-8")
    right = (tmp_path / "b" / "plans" / "mnExpectChild" / "source.json").read_text(encoding="utf-8")
    assert left == right
    assert left == dumps(first.to_dict())


def test_flat_node_list_with_parent_ids_is_accepted(tmp_path: Path) -> None:
    data = {
        "token": "mn_flat",
        "version": "9",
        "nodes": [
            {"node_id": "b", "parent_id": "a", "order": 1, "text": "步骤二"},
            {"node_id": "a", "parent_id": None, "order": 0, "text": "用例"},
            {"node_id": "c", "parent_id": "a", "order": 0, "text": "步骤一"},
        ],
    }
    info = {"obj_token": "mn_flat", "obj_type": "mindnote", "version": "9"}
    mindnote = read("https://x.feishu.cn/wiki/wik_1", make_lark(data, info=info), tmp_path)

    assert [node.id for node in mindnote.nodes] == ["a", "c", "b"]
    assert {node.id: node.parent for node in mindnote.nodes}["c"] == "a"


def test_flat_node_list_with_unknown_parent_becomes_root(tmp_path: Path) -> None:
    data = {
        "token": "mn_orphan",
        "version": "1",
        "nodes": [
            {"node_id": "a", "parent_id": "ghost", "text": "用例"},
            {"node_id": "b", "parent_id": "a", "text": "步骤"},
        ],
    }
    info = {"obj_token": "mn_orphan", "obj_type": "mindnote", "version": "1"}
    mindnote = read("https://x.feishu.cn/wiki/wik_1", make_lark(data, info=info), tmp_path)

    assert [node.id for node in mindnote.nodes] == ["a", "b"]
    assert mindnote.nodes[0].parent is None


def test_version_falls_back_to_content_hash(tmp_path: Path) -> None:
    info = {"obj_token": "mnExpectChild", "obj_type": "mindnote"}
    first = read("https://x.feishu.cn/wiki/wik_1", make_lark(info=info), tmp_path / "a")
    again = read("https://x.feishu.cn/wiki/wik_1", make_lark(info=info), tmp_path / "b")

    assert first.version == again.version
    assert len(first.version) == 16

    changed = payload()
    changed["nodes"][0]["children"][0]["children"][0]["text"] = "标注工作台（改名）"
    other = read("https://x.feishu.cn/wiki/wik_1", make_lark(changed, info=info), tmp_path / "c")
    assert other.version != first.version


def test_missing_scope_becomes_auth_required_with_link(tmp_path: Path) -> None:
    error = MissingScope(["mindnotes:node:read"], "缺少权限范围")
    lark = make_lark(error=error)

    with pytest.raises(AuthRequired) as caught:
        read("https://x.feishu.cn/wiki/wik_1", lark, tmp_path)

    assert caught.value.url == "https://open.feishu.cn/auth/abc"
    assert "mindnotes:node:read" in str(caught.value)
    assert ("run", ("auth", "login", "--no-wait", "--scope", "mindnotes:node:read")) in lark.calls


def test_missing_scope_on_node_list_also_asks_for_auth(tmp_path: Path) -> None:
    error = MissingScope(["mindnotes:node:read"], "缺少权限范围")
    with pytest.raises(AuthRequired) as caught:
        read("https://x.feishu.cn/wiki/wik_1", make_lark(error=error, error_at="nodes"), tmp_path)

    assert caught.value.url == "https://open.feishu.cn/auth/abc"


def test_missing_scope_without_link_keeps_empty_url(tmp_path: Path) -> None:
    error = MissingScope([], "缺少权限范围")
    lark = make_lark(error=error, auth={"ok": True, "data": {}})

    with pytest.raises(AuthRequired) as caught:
        read("https://x.feishu.cn/wiki/wik_1", lark, tmp_path)

    assert caught.value.url == ""


def test_missing_scope_with_broken_auth_call_keeps_empty_url(tmp_path: Path) -> None:
    error = MissingScope(["a"], "缺少权限范围")
    fake = make_lark(error=error)

    def broken(args: list[str]) -> dict[str, Any]:
        raise LarkError("lark-cli auth login 失败")

    fake.run = broken  # type: ignore[method-assign]
    with pytest.raises(AuthRequired) as caught:
        read("https://x.feishu.cn/wiki/wik_1", fake, tmp_path)

    assert caught.value.url == ""


def test_no_permission_is_not_converted(tmp_path: Path) -> None:
    with pytest.raises(NoPermission):
        read("https://x.feishu.cn/wiki/wik_1", make_lark(error=NoPermission("没有权限")), tmp_path)


def test_non_mindnote_link_is_rejected(tmp_path: Path) -> None:
    info = {"obj_token": "docx_1", "obj_type": "docx"}
    with pytest.raises(LarkError) as caught:
        read("https://x.feishu.cn/wiki/wik_1", make_lark(info=info), tmp_path)
    assert "思维笔记" in str(caught.value)
    assert not isinstance(caught.value, (AuthRequired, NoPermission))


def test_missing_token_is_reported(tmp_path: Path) -> None:
    info = {"obj_type": "mindnote"}
    with pytest.raises(LarkError) as caught:
        read("https://x.feishu.cn/wiki/wik_1", make_lark(info=info), tmp_path)
    assert "token" in str(caught.value)


def test_token_cannot_be_used_as_directory_name(tmp_path: Path) -> None:
    info = {"obj_token": "../escape", "obj_type": "mindnote"}
    with pytest.raises(ValueError):
        read("https://x.feishu.cn/wiki/wik_1", make_lark(info=info), tmp_path)


def test_diff_reports_added_changed_and_removed() -> None:
    old = Mindnote(
        token="mn",
        version="1",
        nodes=(
            MindNode("a", None, 0, "模块"),
            MindNode("b", "a", 0, "旧文字"),
            MindNode("c", "a", 1, "被删掉的"),
        ),
    )
    new = Mindnote(
        token="mn",
        version="2",
        nodes=(
            MindNode("a", None, 0, "模块"),
            MindNode("b", "a", 0, "新文字"),
            MindNode("d", "a", 2, "新增的"),
        ),
    )

    assert diff(old, new) == {"added": ("d",), "changed": ("b",), "removed": ("c",)}
    assert diff(old, old) == {"added": (), "changed": (), "removed": ()}


def test_diff_ignores_reorder_from_an_inserted_sibling() -> None:
    old = Mindnote(
        token="mn",
        version="1",
        nodes=(
            MindNode("m", None, 0, "模块"),
            MindNode("a", "m", 0, "用例 A"),
            MindNode("b", "m", 1, "用例 B"),
            MindNode("c", "m", 2, "用例 C"),
        ),
    )
    new = Mindnote(
        token="mn",
        version="2",
        nodes=(
            MindNode("m", None, 0, "模块"),
            MindNode("z", "m", 0, "新用例"),
            MindNode("a", "m", 1, "用例 A"),
            MindNode("b", "m", 2, "用例 B"),
            MindNode("c", "m", 3, "用例 C"),
        ),
    )

    assert diff(old, new) == {"added": ("z",), "changed": (), "removed": ()}


def test_diff_marks_a_moved_node_as_changed() -> None:
    old = Mindnote(
        token="mn",
        version="1",
        nodes=(MindNode("m", None, 0, "模块"), MindNode("f", "m", 0, "功能"), MindNode("a", "m", 1, "用例")),
    )
    new = Mindnote(
        token="mn",
        version="2",
        nodes=(MindNode("m", None, 0, "模块"), MindNode("f", "m", 0, "功能"), MindNode("a", "f", 0, "用例")),
    )

    assert diff(old, new) == {"added": (), "changed": ("a",), "removed": ()}


def test_mindnote_from_dict_is_strict() -> None:
    body = {"token": "mn", "version": "1", "nodes": [{"id": "a", "parent": None, "order": 0, "text": "x"}]}
    assert Mindnote.from_dict(body).token == "mn"

    with pytest.raises(ValueError, match="未知字段"):
        Mindnote.from_dict({**body, "extra": 1})
    with pytest.raises(ValueError, match="缺少必填字段"):
        Mindnote.from_dict({"token": "mn", "version": "1"})
    with pytest.raises(ValueError, match="parent"):
        Mindnote.from_dict(
            {"token": "mn", "version": "1", "nodes": [{"id": "a", "parent": 3, "order": 0, "text": "x"}]}
        )
