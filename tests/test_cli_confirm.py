from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jev_convert import cli, jsonio
from jev_convert import config as config_module
from jev_convert.compile import library
from jev_convert.compile.pipeline import CompileResult
from jev_convert.compile.plan import Entry, Plan, SourceRef, Step, Target
from jev_convert.compile.s0_read import MindNode, Mindnote
from jev_convert.confirm.base import PENDING
from jev_convert.kb.cards import CardElement, PageCard
from jev_convert.kb.navgraph import NavGraph, NavNode
from jev_convert.lark import MissingScope

URL = "https://example.feishu.cn/wiki/wikcnTEST"
TOKEN = "mnT"
AUTH_LINK = "https://open.feishu.cn/auth/xyz"


class FakeLark:
    """confirm CLI 的假飞书访问层：预置表内容，记录写入。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.created: list[dict[str, Any]] = []
        self.updated: dict[str, dict[str, Any]] = {}

    def resolve_wiki(self, url: str) -> dict[str, Any]:
        return {"token": TOKEN}

    def base_records(self, base_token: str, table_id: str) -> list[dict[str, Any]]:
        return self.rows

    def base_create(self, base_token: str, table_id: str, rows: list[dict[str, Any]]) -> int:
        self.created.extend(rows)
        return len(rows)

    def base_update(self, base_token: str, table_id: str,
                    updates: dict[str, dict[str, Any]]) -> int:
        self.updated.update(updates)
        return len(updates)

    def run(self, args: list[str]) -> dict[str, Any]:
        return {"ok": True, "data": {"url": AUTH_LINK}}    # auth login --no-wait 的授权链接


def make_draft_plan(case_id: str = "TC-page-001") -> Plan:
    """一个 draft 计划：目标为空、置信度 0，等人工手填“开始处理”。"""
    return Plan(
        id=case_id,
        title="处理一条任务",
        status="draft",
        source=SourceRef(mindnote=TOKEN, node_id="n_1", version="1"),
        kb_version="webui@test",
        requires=(),
        entry=Entry(node="page", route="url", url="/p", arrive=()),
        steps=(
            Step(seq=1, source_node="n_2", original="点开始处理按钮", page="page", kind="click",
                 goal="点击", target=Target(name="", confidence=0.0), expect=("element=开始处理",),
                 budget=5, status="draft"),
        ),
    )


def make_graph() -> NavGraph:
    return NavGraph(kb_version="webui@test",
                    nodes=(NavNode(id="page", path="/p", title="页面", direct="yes"),), edges=())


def make_cards() -> dict[str, PageCard]:
    return {"page": PageCard(node="page", structure_version="v1", path="/p", title="页面",
                             elements=(CardElement(role="button", label="开始处理"),))}


def write_mindnote(data_dir: Path) -> None:
    mindnote = Mindnote(token=TOKEN, version="1", nodes=(
        MindNode(id="root", parent=None, order=0, text="根"),
        MindNode(id="n1", parent="root", order=1, text="步骤一"),
        MindNode(id="n2", parent="root", order=2, text="步骤二"),
    ))
    folder = data_dir / "plans" / TOKEN
    folder.mkdir(parents=True, exist_ok=True)
    jsonio.dump(mindnote.to_dict(), folder / "source.json")


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, table: bool) -> Path:
    template = Path(__file__).resolve().parent.parent / "qa.example.toml"
    target = tmp_path / "qa.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('data_dir = "qa_data"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace('runs_dir = "qa_runs"', f'runs_dir = "{tmp_path / "runs"}"')
    if table:
        text = text.replace('confirm_base_token = ""', 'confirm_base_token = "baseT"')
        text = text.replace('confirm_table_id = ""', 'confirm_table_id = "tblT"')
    target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(config_module, "DEFAULT_ENV_PATH", tmp_path / ".env")
    (tmp_path / "data" / "kb").mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_config(tmp_path, monkeypatch, table=True)


@pytest.fixture
def lark(monkeypatch: pytest.MonkeyPatch) -> FakeLark:
    fake = FakeLark()
    monkeypatch.setattr(cli, "make_lark", lambda config: fake)
    return fake


def test_confirm_collect_updates_plan_and_writes_back(
    configured: Path, lark: FakeLark, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr("jev_convert.kb.load.load_graph", lambda _: make_graph())
    monkeypatch.setattr("jev_convert.kb.load.load_cards", lambda _: make_cards())
    data_dir = configured.parent / "data"
    plan = make_draft_plan()
    library.write(library.plans_dir(data_dir, TOKEN), [plan])
    key = f"{plan.id}#1@{library.content_hash(plan)}"
    lark.rows = [{"record_id": "r1",
                  "fields": {"键": key, "结论": "手填", "手填目标": "开始处理", "状态": PENDING}}]

    code = cli.main(["confirm", URL])
    captured = capsys.readouterr()

    assert code == 0
    saved = library.load(library.plans_dir(data_dir, TOKEN))[plan.id]
    assert saved.status == "confirmed"
    assert saved.steps[0].target.name == "开始处理"
    assert lark.updated["r1"]["状态"] == ["已处理"]
    confirmed = json.loads((data_dir / "kb" / "confirmed.json").read_text(encoding="utf-8"))
    assert confirmed["pages"]["page"]["aliases"]
    assert f"{plan.id}：confirmed" in captured.out
    assert "回写确认表 1 行" in captured.out


def test_confirm_requires_table_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    write_config(tmp_path, monkeypatch, table=False)

    code = cli.main(["confirm", URL])
    captured = capsys.readouterr()

    assert code == 2
    assert "未配置确认表" in captured.err


def test_confirm_missing_scope_prints_auth_link(
    configured: Path, lark: FakeLark, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    plan = make_draft_plan()
    library.write(library.plans_dir(configured.parent / "data", TOKEN), [plan])
    monkeypatch.setattr("jev_convert.kb.load.load_graph", lambda _: make_graph())
    monkeypatch.setattr("jev_convert.kb.load.load_cards", lambda _: make_cards())

    def raise_missing(base_token: str, table_id: str) -> list[dict[str, Any]]:
        raise MissingScope(scopes=("base:record:read",), message="缺 scope")

    lark.base_records = raise_missing

    code = cli.main(["confirm", URL])
    captured = capsys.readouterr()

    assert code == 1
    assert AUTH_LINK in captured.err


def test_confirm_empty_library_returns_2(configured: Path, lark: FakeLark, capsys) -> None:
    code = cli.main(["confirm", URL])
    captured = capsys.readouterr()

    assert code == 2
    assert "计划库" in captured.err


def test_compile_exports_confirm_items(
    configured: Path, lark: FakeLark, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr("jev_convert.kb.load.load_graph", lambda _: make_graph())
    monkeypatch.setattr("jev_convert.kb.load.load_cards", lambda _: make_cards())
    result = CompileResult(plans=(make_draft_plan(),), confirmed=0, draft=1, pending=1,
                           mindnote=TOKEN, fit=0.8)
    monkeypatch.setattr(cli, "compile_mindnote", lambda *args, **kwargs: result)

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()

    assert code == 0
    assert len(lark.created) == 1
    assert lark.created[0]["键"].startswith("TC-page-001#1@")
    assert lark.created[0]["状态"] == [PENDING]
    assert "确认表：新写入 1 项" in captured.out


def test_compile_skips_export_without_table(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(configured.parent, monkeypatch, table=False)
    result = CompileResult(plans=(make_draft_plan(),), confirmed=0, draft=1, pending=1,
                           mindnote=TOKEN, fit=0.8)
    monkeypatch.setattr(cli, "compile_mindnote", lambda *args, **kwargs: result)

    def fail_make_lark(config: Any) -> None:
        raise AssertionError("表未配置时不应创建 Lark")

    monkeypatch.setattr(cli, "make_lark", fail_make_lark)

    assert cli.main(["compile", URL]) == 0


def test_compile_exports_labels_when_fit_low(
    configured: Path, lark: FakeLark, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_mindnote(configured.parent / "data")
    result = CompileResult(plans=(), confirmed=0, draft=0, pending=2, mindnote=TOKEN, fit=0.3)
    monkeypatch.setattr(cli, "compile_mindnote", lambda *args, **kwargs: result)

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()

    assert code == 0
    assert len(lark.created) == 2
    assert lark.created[0]["键"] == f"标注#{TOKEN}#n1"
    assert "确认表：新写入 2 项" in captured.out
