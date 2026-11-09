"""飞书访问层：所有 lark-cli 调用集中在这里，错误统一分类。

命令固定追加 `--format json`。lark-cli 的成功信封写在 stdout：

    {"ok": true, "identity": "user", "data": {...}, "meta": {"count": 1}}

失败信封写在 stderr，退出码非 0：

    {"ok": false, "identity": "user", "error": {"type": "api", "subtype": "...",
     "code": 99991679, "message": "...", "hint": "..."}}

`run()` 返回完整信封，封装方法返回信封里的 `data`。
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

FORMAT_ARGS = ("--format", "json")
RETRY_DELAYS_S = (1, 2, 4)

# 这些参数的值可以进日志（只是 token / 链接）；其他参数的值一律省略。
SAFE_FLAGS = frozenset({"--node-token", "--mindnote-id", "--parent-token", "--doc", "--token", "--base-token",
                        "--table-id", "--limit", "--offset"})
UNVALUED_FLAGS = frozenset({"--format", "--json", "--dry-run", "--full-comment"})

PERMISSION_SUBTYPES = frozenset({"permission_denied", "access_denied", "no_permission", "forbidden"})
PERMISSION_TYPES = frozenset({"permission", "permission_denied", "forbidden", "unauthorized"})
RATE_LIMIT_SUBTYPES = frozenset({"rate_limit", "rate_limited", "too_many_requests", "throttled"})
RATE_LIMIT_TYPES = frozenset({"rate_limit", "rate_limited", "too_many_requests"})
NODE_LIST_KEYS = ("nodes", "items", "children")


class LarkError(Exception):
    """lark-cli 调用失败。信息里写明是哪条命令、lark-cli 给出的原因。"""


class MissingScope(LarkError):
    """应用未被授权访问所需的权限范围；调用方据此申请增量授权。"""

    def __init__(self, scopes: Sequence[str], message: str = "") -> None:
        self._scopes = tuple(str(scope) for scope in scopes)
        super().__init__(message or f"缺少权限范围：{', '.join(self._scopes) or '未知'}")

    @property
    def scopes(self) -> tuple[str, ...]:
        return self._scopes


class NoPermission(LarkError):
    """当前身份没有该文档、知识库或文件夹的权限。"""


class RateLimited(LarkError):
    """被飞书限流；已按 1、2、4 秒重试仍未成功。"""


def _command(args: Sequence[str]) -> str:
    return " ".join(args[:2]) or "lark-cli"


def _redact(argv: Sequence[str]) -> str:
    """命令名和 token 进日志，其余参数值省略，避免把文档内容写进日志。"""
    shown: list[str] = []
    hide_value = False
    items = list(argv)
    for position, item in enumerate(items):
        if hide_value:
            shown.append("<省略>")
            hide_value = False
            continue
        shown.append(item)
        if item in SAFE_FLAGS:
            continue
        following = items[position + 1] if position + 1 < len(items) else ""
        if item in UNVALUED_FLAGS and (not following or following.startswith("--")):
            continue        # --json 在 base 命令里带值（记录内容），在别处是开关
        if item.startswith("--"):
            hide_value = True
    return " ".join(shown)


def _first_envelope(text: str) -> Any:
    """取出输出里的 JSON 信封：lark-cli 会先往同一个流里写 "[lark-cli] [WARN] ..." 之类的日志行。"""
    decoder = json.JSONDecoder()
    fallback: Any = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "ok" in value:
            return value
        if fallback is None:
            fallback = value
    return fallback


def _parse(output: str, args: Sequence[str]) -> dict:
    text = (output or "").strip()
    if not text:
        raise LarkError(f"lark-cli {_command(args)} 没有输出")
    envelope = _first_envelope(text)
    if envelope is None:
        raise LarkError(f"lark-cli {_command(args)} 的输出不是 JSON：{text[:200]}")
    if not isinstance(envelope, dict):
        raise LarkError(f"lark-cli {_command(args)} 的输出不是 JSON 对象")
    return envelope


def _error_of(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    error = envelope.get("error")
    return error if isinstance(error, Mapping) else {}


def _detail(error: Mapping[str, Any]) -> str:
    label = "/".join(str(error[key]) for key in ("type", "subtype") if error.get(key))
    text = f"{label}: {error.get('message', '')}" if label else str(error.get("message", ""))
    hint = error.get("hint")
    return f"{text}（{hint}）" if hint else text.strip(": ")


def _scopes(error: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("missing_scopes", "scopes", "required_scopes"):
        value = error.get(key)
        if isinstance(value, str) and value.strip():
            return tuple(part for part in value.replace(",", " ").split() if part)
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value if str(item))
    return ()


def _is_rate_limit(error: Mapping[str, Any]) -> bool:
    kind = str(error.get("type", "")).lower()
    subtype = str(error.get("subtype", "")).lower()
    codes = (error.get("code"), error.get("status"), error.get("http_status"))
    return (
        kind in RATE_LIMIT_TYPES
        or subtype in RATE_LIMIT_SUBTYPES
        or any(code == 429 or code == "429" for code in codes)
    )


def _is_permission(error: Mapping[str, Any]) -> bool:
    kind = str(error.get("type", "")).lower()
    subtype = str(error.get("subtype", "")).lower()
    return subtype in PERMISSION_SUBTYPES or kind in PERMISSION_TYPES


def _classify(envelope: Mapping[str, Any], args: Sequence[str]) -> LarkError:
    error = _error_of(envelope)
    message = f"lark-cli {_command(args)} 失败：{_detail(error)}"
    if str(error.get("subtype", "")).lower() == "missing_scope":
        return MissingScope(_scopes(error), message)
    if _is_permission(error):
        return NoPermission(message)
    if _is_rate_limit(error):
        return RateLimited(message)
    return LarkError(message)


def _data_of(envelope: Mapping[str, Any]) -> dict:
    data = envelope.get("data")
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise LarkError(f"lark-cli 返回的 data 不是 JSON 对象：{type(data).__name__}")
    return dict(data)


def _node_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, Mapping):
        items = next((data[key] for key in NODE_LIST_KEYS if isinstance(data.get(key), list)), [])
    else:
        items = []
    return [dict(item) for item in items if isinstance(item, Mapping)]


def _run_cli(argv: list[str], timeout_s: int) -> str:
    """默认 runner：同步执行 lark-cli，返回 stdout（为空时退回 stderr，失败信封在那里）。"""
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    return completed.stdout.strip() or completed.stderr.strip()


class Lark:
    """lark-cli 的薄封装：固定 `--format json`，统一错误分类，限流与超时按 1、2、4 秒重试 3 次。"""

    def __init__(
        self,
        cli: str = "lark-cli",
        timeout_s: int = 60,
        runner: Callable[[list[str], int], str] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._cli = cli
        self._timeout_s = timeout_s
        self._runner = runner if runner is not None else _run_cli
        self._sleep = sleep if sleep is not None else time.sleep

    def run(self, args: Sequence[str]) -> dict:
        """执行 `<cli> <args...> --format json`，返回解析后的 JSON 信封（成功时 ok 为 true）。

        限流与超时按 1、2、4 秒重试 3 次后抛 `RateLimited` / `LarkError`；其他错误不重试。
        """
        argv = [self._cli, *args, *FORMAT_ARGS]
        log.info("lark-cli 调用：%s", _redact(argv))
        attempt = 0
        while True:
            try:
                output = self._runner(argv, self._timeout_s)
            except subprocess.TimeoutExpired as exc:
                if attempt >= len(RETRY_DELAYS_S):
                    raise LarkError(f"lark-cli {_command(args)} 超时 {self._timeout_s}s，重试后仍失败") from exc
                log.warning("lark-cli %s 超时，%ss 后重试", _command(args), RETRY_DELAYS_S[attempt])
                self._sleep(RETRY_DELAYS_S[attempt])
                attempt += 1
                continue
            envelope = _parse(output, args)
            if envelope.get("ok") is True:
                return envelope
            error = _classify(envelope, args)
            if isinstance(error, RateLimited) and attempt < len(RETRY_DELAYS_S):
                log.warning("lark-cli %s 被限流，%ss 后重试", _command(args), RETRY_DELAYS_S[attempt])
                self._sleep(RETRY_DELAYS_S[attempt])
                attempt += 1
                continue
            raise error

    def resolve_wiki(self, url: str) -> dict:
        """解析 wiki 链接：`lark-cli wiki +node-get --node-token <url>`。

        返回 data，含 node_token、obj_token、obj_type、space_id、title 等；
        obj_type 为 mindnote 时，用 obj_token 调 `mindnote_nodes()`。
        """
        return _data_of(self.run(["wiki", "+node-get", "--node-token", url]))

    def mindnote_nodes(self, token: str) -> list[dict]:
        """拉思维笔记节点树：`lark-cli mindnotes nodes list --mindnote-id <token>`。

        返回 data.nodes（每个节点含 node_id、parent_id、children、text 等）。
        """
        return _node_list(_data_of(self.run(["mindnotes", "nodes", "list", "--mindnote-id", token])))

    def create_doc(self, folder_token: str, title: str, markdown: str) -> dict:
        """在云空间文件夹里新建 Markdown 文档：
        `lark-cli docs +create --parent-token <folder_token> --title <title> --content <markdown>
        --doc-format markdown`。返回 data，含 document_id 与 url。
        """
        return _data_of(
            self.run(
                [
                    "docs",
                    "+create",
                    "--parent-token",
                    folder_token,
                    "--title",
                    title,
                    "--content",
                    markdown,
                    "--doc-format",
                    "markdown",
                ]
            )
        )

    def add_comment(self, token: str, text: str) -> dict:
        """给文档加全文评论：`lark-cli drive +add-comment --doc <token> --content <reply_elements JSON>`。

        `text` 被包成 reply_elements：`[{"type": "text", "text": "<text>"}]`。
        """
        content = json.dumps([{"type": "text", "text": text}], ensure_ascii=False)
        return _data_of(self.run(["drive", "+add-comment", "--doc", token, "--content", content]))

    # ---------- 多维表格（确认表，Step 13） ----------
    def base_records(self, base_token: str, table_id: str) -> list[dict]:
        """读出整张表：`lark-cli base +record-list --base-token <t> --table-id <id> --limit 200 --offset <n>`。

        返回 [{"record_id": str, "fields": {字段名: 值}}]；兼容按记录列表和按列矩阵两种返回形状。
        """
        found: list[dict] = []
        offset = 0
        while True:
            data = _data_of(self.run(["base", "+record-list", "--base-token", base_token, "--table-id", table_id,
                                      "--limit", str(BASE_PAGE), "--offset", str(offset)]))
            page = _records(data)
            found.extend(page)
            if not data.get("has_more") or not page:
                return found
            offset += len(page)

    def base_create(self, base_token: str, table_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """`lark-cli base +record-batch-create --json {"create_records": [...]}`，每批最多 200 条。返回写入条数。"""
        for start in range(0, len(rows), BASE_PAGE):
            body = json.dumps({"create_records": [dict(row) for row in rows[start:start + BASE_PAGE]]},
                              ensure_ascii=False)
            self.run(["base", "+record-batch-create", "--base-token", base_token, "--table-id", table_id,
                      "--json", body])
        return len(rows)

    def base_update(self, base_token: str, table_id: str, updates: Mapping[str, Mapping[str, Any]]) -> int:
        """`lark-cli base +record-batch-update --json {"update_records": {record_id: {...}}}`，每批最多 200 条。"""
        items = list(updates.items())
        for start in range(0, len(items), BASE_PAGE):
            batch = {record_id: dict(fields) for record_id, fields in items[start:start + BASE_PAGE]}
            body = json.dumps({"update_records": batch}, ensure_ascii=False)
            self.run(["base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id,
                      "--json", body])
        return len(items)


BASE_PAGE = 200


def _records(data: Mapping[str, Any]) -> list[dict]:
    """把 +record-list 的返回整理成 [{"record_id", "fields"}]。

    认两种形状：记录列表（items / records，每项含 record_id 和 fields），
    或按列矩阵（fields 是列名列表，data / rows 是行，record_id_list 给出每行的 id）。
    """
    for key in ("items", "records"):
        items = data.get(key)
        if isinstance(items, list):
            rows = []
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, Mapping):
                    fields = {k: v for k, v in item.items() if k not in ("record_id", "id")}
                rows.append({"record_id": str(item.get("record_id") or item.get("id") or ""), "fields": dict(fields)})
            return rows
    columns = data.get("fields") or data.get("columns")
    matrix = data.get("data") or data.get("rows")
    ids = data.get("record_id_list") or data.get("record_ids") or []
    if isinstance(columns, list) and isinstance(matrix, list):
        names = [str(item.get("name", item.get("field_name", ""))) if isinstance(item, Mapping) else str(item)
                 for item in columns]
        rows = []
        for position, values in enumerate(matrix):
            if not isinstance(values, list):
                continue
            record_id = str(ids[position]) if position < len(ids) else ""
            rows.append({"record_id": record_id, "fields": dict(zip(names, values))})
        return rows
    return []
