"""计划库（Step 9）：写盘可复现、索引、增量合并、compile 写盘与 llm stats。"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from qa_support import make_plan, make_step

from jev_convert import cli, jsonio
from jev_convert import config as config_module
from jev_convert.compile import library
from jev_convert.compile.pipeline import CompileResult
from jev_convert.compile.plan import SourceRef


def plan(case_id="TC-pool-001", node="n_1", status="draft", name="开始处理", kb="webui@test", goal="点击“开始处理”"):
    item = make_plan(make_step(name=name, goal=goal), case_id=case_id, status=status)
    item = replace(item, source=SourceRef(mindnote="mnA", node_id=node, version="1"), kb_version=kb)
    return library.with_status(item, status)


def test_write_twice_is_byte_identical_and_indexed(tmp_path: Path) -> None:
    library.write(tmp_path, [plan("TC-b", "n_2"), plan("TC-a")])
    first = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    library.write(tmp_path, [plan("TC-a"), plan("TC-b", "n_2")])
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == first
    index = jsonio.load(tmp_path / "index.json")["cases"]
    assert list(index) == ["TC-a", "TC-b"]
    assert index["TC-b"]["node_id"] == "n_2" and index["TC-b"]["status"] == "draft"


def test_hash_ignores_status_and_learned_locator() -> None:
    base = plan()
    learned = replace(base, steps=(replace(base.steps[0], target=replace(base.steps[0].target, locator="k")),))
    assert library.content_hash(base) == library.content_hash(library.with_status(learned, "confirmed"))
    assert library.content_hash(base) != library.content_hash(plan(name="批量退回"))


def test_merge_keeps_confirmed_plan_when_content_is_unchanged() -> None:
    confirmed = plan(status="confirmed")
    result = library.merge({confirmed.id: confirmed}, [plan()], live_nodes={"n_1"}, kb_version="webui@test")
    assert result.kept == ("TC-pool-001",) and result.plans[0].status == "confirmed"


def test_merge_keeps_human_edits_when_only_the_compiled_target_differs() -> None:
    # 人工在确认表里把目标改成了“批量退回”；导图原文没变，重新编译出来的还是“开始处理”，不能覆盖
    edited = plan(status="confirmed", name="批量退回")
    result = library.merge({edited.id: edited}, [plan()], live_nodes={"n_1"}, kb_version="webui@test")
    assert result.kept == ("TC-pool-001",)
    assert result.plans[0].steps[0].target.name == "批量退回"


def test_merge_recompiles_when_kb_version_changes() -> None:
    old = plan(status="confirmed", kb="webui@old")
    result = library.merge({old.id: old}, [plan()], live_nodes={"n_1"}, kb_version="webui@test")
    assert result.changed == ("TC-pool-001",) and result.plans[0].status == "draft"


def test_merge_replaces_changed_plan_and_adds_new_one() -> None:
    old = plan(status="confirmed")
    changed = plan(goal="点击“开始处理”并确认")      # 导图原文改了
    result = library.merge({old.id: old}, [changed, plan("TC-new", "n_9")], live_nodes={"n_1", "n_9"},
                           kb_version="webui@test")
    assert result.changed == ("TC-pool-001",) and result.added == ("TC-new",)
    assert {item.id: item.status for item in result.plans} == {"TC-new": "draft", "TC-pool-001": "draft"}


def test_merge_marks_deleted_and_outdated_plans_stale() -> None:
    gone = plan("TC-gone", "n_gone", status="confirmed")
    old_kb = plan("TC-old", "n_old", status="confirmed", kb="webui@old")
    kept = plan("TC-keep", "n_keep", status="confirmed")
    existing = {item.id: item for item in (gone, old_kb, kept)}
    result = library.merge(existing, [], live_nodes={"n_old", "n_keep"}, kb_version="webui@test")
    assert set(result.stale) == {"TC-gone", "TC-old"}
    statuses = {item.id: item.status for item in result.plans}
    assert statuses == {"TC-gone": "stale", "TC-old": "stale", "TC-keep": "confirmed"}
    assert all(step.status == "stale" for item in result.plans if item.status == "stale" for step in item.steps)


def test_save_groups_by_mindnote_and_uses_source_json(tmp_path: Path) -> None:
    folder = library.plans_dir(tmp_path, "mnA")
    jsonio.dump({"token": "mnA", "version": "1", "nodes": [{"id": "n_1", "parent": None, "order": 0, "text": "x"}]},
                folder / "source.json")
    library.write(folder, [plan("TC-gone", "n_gone", status="confirmed")])
    results = library.save(tmp_path, [plan()], kb_version="webui@test")
    assert results["mnA"].added == ("TC-pool-001",) and results["mnA"].stale == ("TC-gone",)
    assert sorted(path.name for path in folder.iterdir()) == ["TC-gone.json", "TC-pool-001.json", "index.json",
                                                               "source.json"]


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    template = Path(__file__).resolve().parent.parent / "qa.example.toml"
    text = template.read_text(encoding="utf-8")
    text = text.replace('data_dir = "qa_data"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace('runs_dir = "qa_runs"', f'runs_dir = "{tmp_path / "runs"}"')
    target = tmp_path / "qa.toml"
    target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(config_module, "DEFAULT_ENV_PATH", tmp_path / ".env")
    return tmp_path


def test_compile_writes_plans_into_the_library_and_records_llm_stats(configured: Path, monkeypatch, capsys) -> None:
    def fake_compile(url: str, config: Any, *, until: str = "s6", llm: Any = None, lark: Any = None,
                 progress: Any = None) -> CompileResult:
        llm.hits, llm.misses = 3, 1
        return CompileResult(plans=(plan(),), draft=1, pending=1)

    monkeypatch.setattr(cli, "compile_mindnote", fake_compile)
    assert cli.main(["compile", "https://example.feishu.cn/wiki/x"]) == 0
    assert (configured / "data" / "plans" / "mnA" / "TC-pool-001.json").exists()
    assert "新增 1" in capsys.readouterr().out
    assert cli.main(["llm", "stats"]) == 0
    out = capsys.readouterr().out
    assert "命中 3，请求 1，命中率 75%" in out and "缓存条目：0" in out
