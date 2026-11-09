import json
import logging
import subprocess
from typing import Any

import pytest

from jev_convert.lark import Lark, LarkError, MissingScope, NoPermission, RateLimited


class FakeRunner:
    """按顺序返回预置输出；元素是异常实例时抛出。用完后重复最后一项。"""

    def __init__(self, *outputs: Any) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self._outputs = list(outputs) or [ok_envelope()]

    def __call__(self, argv: list[str], timeout_s: int) -> str:
        self.calls.append((list(argv), timeout_s))
        item = self._outputs[min(len(self.calls), len(self._outputs)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


class FakeSleep:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def envelope(**fields: Any) -> str:
    payload: dict[str, Any] = {"ok": False, "identity": "user", "error": {"type": "api", "message": "出错了"}}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def error_envelope(**error: Any) -> str:
    return envelope(error={"type": "api", "message": "出错了", **error})


def ok_envelope(**data: Any) -> str:
    return json.dumps({"ok": True, "identity": "user", "data": {"obj_token": "obj_1", **data}}, ensure_ascii=False)


def make_lark(
    *outputs: Any, cli: str = "lark-cli", timeout_s: int = 60
) -> tuple[Lark, FakeRunner, FakeSleep]:
    runner = FakeRunner(*outputs)
    sleep = FakeSleep()
    return Lark(cli=cli, timeout_s=timeout_s, runner=runner, sleep=sleep), runner, sleep


def test_run_returns_envelope_and_appends_format_json_last() -> None:
    lark, runner, _ = make_lark(ok_envelope())
    result = lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert result["ok"] is True
    assert result["data"]["obj_token"] == "obj_1"
    assert runner.argvs == [[
        "lark-cli",
        "wiki",
        "+node-get",
        "--node-token",
        "wik_1",
        "--format",
        "json",
    ]]


def test_run_uses_custom_cli_and_timeout() -> None:
    lark, runner, _ = make_lark(cli="/opt/bin/lark-cli", timeout_s=7)
    lark.run(["docs", "+create"])
    assert runner.calls == [(["/opt/bin/lark-cli", "docs", "+create", "--format", "json"], 7)]


def test_envelope_is_extracted_after_cli_log_lines() -> None:
    noisy = "[lark-cli] [WARN] token storage is not writable\n" + ok_envelope()
    lark, _, _ = make_lark(noisy)
    assert lark.run(["wiki", "+node-get", "--node-token", "wik_1"])["data"]["obj_token"] == "obj_1"


def test_missing_scope_exposes_scopes() -> None:
    lark, runner, sleep = make_lark(
        error_envelope(type="authorization", subtype="missing_scope", code=99991679,
                       missing_scopes=["docx:document:readonly", "wiki:wiki:readonly"])
    )
    with pytest.raises(MissingScope) as caught:
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert caught.value.scopes == ("docx:document:readonly", "wiki:wiki:readonly")
    assert "missing_scope" in str(caught.value)
    assert len(runner.calls) == 1
    assert sleep.waits == []


def test_missing_scope_accepts_space_separated_scope_string() -> None:
    lark, _, _ = make_lark(
        error_envelope(subtype="missing_scope", scopes="docx:document mindnotes:node:read")
    )
    with pytest.raises(MissingScope) as caught:
        lark.run(["mindnotes", "nodes", "list", "--mindnote-id", "mn_1"])
    assert caught.value.scopes == ("docx:document", "mindnotes:node:read")


def test_missing_scope_without_scope_list_is_still_missing_scope() -> None:
    lark, _, _ = make_lark(error_envelope(subtype="missing_scope"))
    with pytest.raises(MissingScope) as caught:
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert caught.value.scopes == ()


@pytest.mark.parametrize(
    "error",
    [
        {"type": "authorization", "subtype": "permission_denied", "code": 131006},
        {"type": "forbidden", "message": "no permission on this doc"},
    ],
)
def test_no_permission_is_not_retried(error: dict[str, Any]) -> None:
    lark, runner, sleep = make_lark(envelope(error=error))
    with pytest.raises(NoPermission):
        lark.run(["docs", "+create", "--parent-token", "fld_1"])
    assert len(runner.calls) == 1
    assert sleep.waits == []


def test_rate_limited_retries_three_times_then_raises() -> None:
    lark, runner, sleep = make_lark(error_envelope(type="rate_limit", code=429))
    with pytest.raises(RateLimited):
        lark.run(["mindnotes", "nodes", "list", "--mindnote-id", "mn_1"])
    assert sleep.waits == [1, 2, 4]
    assert len(runner.calls) == 4
    assert all(argv[-2:] == ["--format", "json"] for argv in runner.argvs)


def test_rate_limited_retry_succeeds_on_second_attempt() -> None:
    lark, runner, sleep = make_lark(error_envelope(subtype="rate_limited"), ok_envelope())
    result = lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert result["ok"] is True
    assert sleep.waits == [1]
    assert len(runner.calls) == 2


def test_http_429_without_rate_limit_type_is_retried() -> None:
    lark, runner, sleep = make_lark(envelope(error={"type": "api", "code": 429, "message": "429 Too Many Requests"}))
    with pytest.raises(RateLimited):
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert sleep.waits == [1, 2, 4]
    assert len(runner.calls) == 4


def test_timeout_is_retried_three_times_then_wrapped() -> None:
    expired = subprocess.TimeoutExpired(cmd=["lark-cli"], timeout=60)
    lark, runner, sleep = make_lark(expired)
    with pytest.raises(LarkError) as caught:
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert not isinstance(caught.value, (MissingScope, NoPermission, RateLimited))
    assert "超时" in str(caught.value)
    assert sleep.waits == [1, 2, 4]
    assert len(runner.calls) == 4


def test_timeout_then_success_returns_envelope() -> None:
    expired = subprocess.TimeoutExpired(cmd=["lark-cli"], timeout=60)
    lark, runner, sleep = make_lark(expired, ok_envelope())
    assert lark.run(["wiki", "+node-get", "--node-token", "wik_1"])["ok"] is True
    assert sleep.waits == [1]
    assert len(runner.calls) == 2


def test_other_error_is_not_retried() -> None:
    lark, runner, sleep = make_lark(error_envelope(subtype="not_found", code=1061044, message="it is not exist"))
    with pytest.raises(LarkError) as caught:
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert not isinstance(caught.value, (MissingScope, NoPermission, RateLimited))
    assert "not_found" in str(caught.value)
    assert len(runner.calls) == 1
    assert sleep.waits == []


@pytest.mark.parametrize("output", ["", "not json at all", "[1, 2]"])
def test_bad_output_is_not_retried(output: str) -> None:
    lark, runner, sleep = make_lark(output)
    with pytest.raises(LarkError):
        lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert len(runner.calls) == 1
    assert sleep.waits == []


def test_resolve_wiki_returns_data() -> None:
    lark, runner, _ = make_lark(ok_envelope(obj_token="mn_1", obj_type="mindnote"))
    assert lark.resolve_wiki("https://feishu.cn/wiki/wik_1")["obj_type"] == "mindnote"
    assert runner.argvs == [[
        "lark-cli",
        "wiki",
        "+node-get",
        "--node-token",
        "https://feishu.cn/wiki/wik_1",
        "--format",
        "json",
    ]]


def test_mindnote_nodes_returns_node_list() -> None:
    lark, runner, _ = make_lark(
        json.dumps({"ok": True, "data": {"nodes": [{"node_id": "n1", "text": "登录"}, {"node_id": "n2"}]}})
    )
    nodes = lark.mindnote_nodes("mn_1")
    assert [node["node_id"] for node in nodes] == ["n1", "n2"]
    assert runner.argvs == [[
        "lark-cli",
        "mindnotes",
        "nodes",
        "list",
        "--mindnote-id",
        "mn_1",
        "--format",
        "json",
    ]]


def test_mindnote_nodes_tolerates_missing_data() -> None:
    lark, _, _ = make_lark(json.dumps({"ok": True}))
    assert lark.mindnote_nodes("mn_1") == []


def test_create_doc_sends_markdown() -> None:
    lark, runner, _ = make_lark(json.dumps({"ok": True, "data": {"document_id": "doc_1", "url": "https://x"}}))
    created = lark.create_doc("fld_1", "2026-01-01 报告", "# 报告\n\n全部通过")
    assert created["document_id"] == "doc_1"
    assert runner.argvs == [[
        "lark-cli",
        "docs",
        "+create",
        "--parent-token",
        "fld_1",
        "--title",
        "2026-01-01 报告",
        "--content",
        "# 报告\n\n全部通过",
        "--doc-format",
        "markdown",
        "--format",
        "json",
    ]]


def test_add_comment_wraps_text_as_reply_elements() -> None:
    lark, runner, _ = make_lark(json.dumps({"ok": True, "data": {"comment_id": "c_1"}}))
    assert lark.add_comment("docx_token", "第 3 步失败")["comment_id"] == "c_1"
    argv = runner.argvs[0]
    assert argv[:5] == ["lark-cli", "drive", "+add-comment", "--doc", "docx_token"]
    assert json.loads(argv[argv.index("--content") + 1]) == [{"type": "text", "text": "第 3 步失败"}]
    assert argv[-2:] == ["--format", "json"]


def test_log_records_command_and_token_but_not_document_body(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    body = "# 秘密内容\n\n这段正文不能进日志"
    lark, _, _ = make_lark(json.dumps({"ok": True, "data": {"document_id": "doc_1"}}))
    lark.create_doc("fld_token", "报告标题", body)
    assert "fld_token" in caplog.text
    assert "docs +create" in caplog.text
    assert "秘密内容" not in caplog.text
    assert "报告标题" not in caplog.text


def test_sleep_is_injected_not_the_real_one() -> None:
    lark, _, sleep = make_lark(error_envelope(type="rate_limit"), ok_envelope())
    lark.run(["wiki", "+node-get", "--node-token", "wik_1"])
    assert sleep.waits == [1]
