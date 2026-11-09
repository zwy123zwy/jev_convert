from pathlib import Path

import pytest

from jev_convert.config import (
    ConfigError,
    load_config,
    load_environment,
)

TEMPLATE = Path(__file__).resolve().parent.parent / "qa.example.toml"


@pytest.fixture
def qa_toml(tmp_path: Path) -> Path:
    target = tmp_path / "qa.toml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_template_loads(qa_toml: Path) -> None:
    config = load_config(qa_toml)
    assert config.target.allowed_hosts == ("localhost", "127.0.0.1")
    assert config.accounts["default"].login == "phone"
    assert config.test_data["graph_definition_id"] == "4"
    assert config.budget.step_actions["click"] == 5
    assert config.thresholds.auto_confirm == 0.9


def test_missing_file_points_at_template(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path / "qa.toml")
    assert "qa.example.toml" in str(error.value)


def test_missing_required_key_names_item(tmp_path: Path) -> None:
    target = tmp_path / "qa.toml"
    target.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace('model = "d3-flash"', ""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as error:
        load_config(target)
    assert "[jev].model" in str(error.value)


def test_cli_override_beats_toml(qa_toml: Path) -> None:
    config = load_config(qa_toml, overrides={"target.base_url": "http://example.test"})
    assert config.target.base_url == "http://example.test"


def test_override_of_unknown_section_raises(qa_toml: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(qa_toml, overrides={"nope.key": "value"})


def test_environment_file_does_not_override_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nQA_SAMPLE=from_file\n\nQA_OTHER=other\n", encoding="utf-8")
    monkeypatch.setenv("QA_SAMPLE", "from_process")
    monkeypatch.delenv("QA_OTHER", raising=False)
    load_environment(env_file)
    import os

    assert os.environ["QA_SAMPLE"] == "from_process"
    assert os.environ["QA_OTHER"] == "other"


def test_repr_has_no_secret_value(qa_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-should-never-appear")
    config = load_config(qa_toml)
    assert "sk-should-never-appear" not in repr(config)


def test_secret_returns_value(qa_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-value")
    config = load_config(qa_toml)
    assert config.secret(config.jev.api_key_env) == "sk-value"


def test_secret_missing_names_variable_only(qa_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = load_config(qa_toml)
    with pytest.raises(ConfigError) as error:
        config.secret(config.jev.api_key_env)
    assert "TYPESAFE_API_KEY" in str(error.value)
    assert ".env" in str(error.value)


def test_new_sections_are_optional(tmp_path: Path) -> None:
    target = tmp_path / "qa.toml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(target)
    assert config.browser.channel == "chrome"
    assert config.session.relogin_max == 1
    assert config.agent.min_confidence == 0.5
    assert config.probes == {}


def test_data_sources_load(tmp_path: Path) -> None:
    target = tmp_path / "qa.toml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8") + """
[data_sources.extra]
page = "pool"
id_column = "任务名称"
""", encoding="utf-8")
    config = load_config(target)
    assert config.data_sources["extra"].id_column == "任务名称"
    assert config.data_sources["extra"].state_column == ""


def test_data_source_without_id_column_names_the_item(tmp_path: Path) -> None:
    target = tmp_path / "qa.toml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8") + '\n[data_sources.bad]\npage = "pool"\n',
                      encoding="utf-8")
    with pytest.raises(ConfigError, match="id_column"):
        load_config(target)
