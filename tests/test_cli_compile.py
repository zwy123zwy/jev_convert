from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jev_convert import cli
from jev_convert import config as config_module
from jev_convert.compile.pipeline import CompileResult
from jev_convert.compile.plan import Entry, Plan, SourceRef, Step, Target
from jev_convert.compile.s0_read import AuthRequired
from jev_convert.lark import LarkError

URL = "https://example.feishu.cn/wiki/wikcnTEST"
KB_VERSION = "webui@test123"


def make_plan(case_id: str) -> Plan:
    return Plan(
        id=case_id,
        title="处理一条任务",
        status="confirmed",
        source=SourceRef(mindnote="mnTest", node_id="n_10", version="1792028614"),
        kb_version=KB_VERSION,
        requires=("logged_in",),
        entry=Entry(node="annotation_task_pool", route="url", url="/workbench/annotation/task-pool",
                    arrive=("element=开始处理",)),
        steps=(
            Step(seq=1, source_node="n_11", original="点击“开始处理”", page="annotation_task_pool", kind="click",
                 goal="点击“开始处理”", target=Target(name="开始处理", confidence=0.95), expect=("element=开始处理",),
                 budget=10, status="confirmed"),
        ),
    )


def make_result() -> CompileResult:
    return CompileResult(
        plans=(make_plan("TC-annotation-task-pool-001"), make_plan("TC-annotation-task-002")),
        confirmed=1,
        draft=1,
        pending=1,
        not_automatable=1,
    )


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    template = Path(__file__).resolve().parent.parent / "qa.example.toml"
    target = tmp_path / "qa.toml"
    # 数据目录、运行目录指到临时目录：compile 会写计划库和 llm-stats.json，不能写进项目自己的 qa_data
    text = template.read_text(encoding="utf-8")
    text = text.replace('data_dir = "qa_data"', f'data_dir = "{tmp_path / "data"}"')
    text = text.replace('runs_dir = "qa_runs"', f'runs_dir = "{tmp_path / "runs"}"')
    target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(config_module, "DEFAULT_ENV_PATH", tmp_path / ".env")
    return target


def summary(output: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in output.splitlines():
        if "：" in line:
            label, _, value = line.partition("：")
            rows[label.strip()] = value.strip()
    return rows


def test_compile_prints_summary(configured: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls: list[tuple[str, str]] = []

    def fake_compile(url: str, config: Any, *, until: str = "s6", llm: Any = None, lark: Any = None,
                 progress: Any = None) -> CompileResult:
        calls.append((url, until))
        return make_result()

    monkeypatch.setattr(cli, "compile_mindnote", fake_compile)

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()
    rows = summary(captured.out)

    assert code == 0
    assert calls == [(URL, "s6")]
    assert rows["用例数"] == "2"
    assert rows["confirmed"] == "1"
    assert rows["draft"] == "1"
    assert rows["待确认数"] == "1"
    assert rows["不可自动化数"] == "1"


def test_compile_passes_until(configured: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    seen: list[str] = []

    def fake_compile(url: str, config: Any, *, until: str = "s6", llm: Any = None, lark: Any = None,
                 progress: Any = None) -> CompileResult:
        seen.append(until)
        return make_result()

    monkeypatch.setattr(cli, "compile_mindnote", fake_compile)

    code = cli.main(["compile", URL, "--until", "s4"])

    assert code == 0
    assert seen == ["s4"]


def test_compile_config_error_returns_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    monkeypatch.setattr(config_module, "DEFAULT_ENV_PATH", tmp_path / ".env")

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()

    assert code == 2
    assert "配置错误" in captured.err


def test_compile_lark_error_returns_1(configured: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_compile(url: str, config: Any, *, until: str = "s6", llm: Any = None, lark: Any = None,
                 progress: Any = None) -> CompileResult:
        raise LarkError("lark-cli 调用失败")

    monkeypatch.setattr(cli, "compile_mindnote", fake_compile)

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()

    assert code == 1
    assert "编译失败" in captured.err


def test_compile_auth_required_returns_1(configured: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fake_compile(url: str, config: Any, *, until: str = "s6", llm: Any = None, lark: Any = None,
                 progress: Any = None) -> CompileResult:
        raise AuthRequired("https://open.feishu.cn/auth/abc", "飞书授权不足")

    monkeypatch.setattr(cli, "compile_mindnote", fake_compile)

    code = cli.main(["compile", URL])
    captured = capsys.readouterr()

    assert code == 1
    assert "https://open.feishu.cn/auth/abc" in captured.err


def test_compile_rejects_unknown_stage(configured: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "compile_mindnote", lambda *args, **kwargs: make_result())

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["compile", URL, "--until", "s9"])

    assert exit_info.value.code == 2


def test_compile_requires_url(configured: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["compile"])

    assert exit_info.value.code == 2


def test_config_check_still_works(configured: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    for name in ("TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY", "QA_ACCOUNT_DEFAULT_PHONE"):
        monkeypatch.delenv(name, raising=False)

    code = cli.main(["config", "check"])
    captured = capsys.readouterr()

    assert code == 1
    assert "[jev].base_url" in captured.out
