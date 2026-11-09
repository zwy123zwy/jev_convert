"""qa.toml / .env 的加载与校验。

读取优先级：命令行参数 > 环境变量（含 .env）> qa.toml > 代码默认值。
密钥不进入 Config 字段：字段里只存环境变量名，取值走 Config.secret()。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "qa.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(Exception):
    """配置缺失或非法。信息里写明缺哪一项、应该写在哪里。"""


def load_environment(path: Path | None = None) -> None:
    """把 .env 写进 os.environ，语义与 jev-ultrafast demo.load_environment() 一致。"""
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Jev:
    base_url: str
    model: str
    api_key_env: str = "TYPESAFE_API_KEY"


@dataclass(frozen=True)
class TextModel:
    base_url: str
    model: str
    api_key_env: str = "TEXT_MODEL_API_KEY"
    reasoning: str = ""


@dataclass(frozen=True)
class CompileLlm:
    base_url: str
    model: str
    api_key_env: str = "TEXT_MODEL_API_KEY"
    temperature: float = 0.0
    timeout_s: int = 60
    prompt_version: str = "v1"


@dataclass(frozen=True)
class Lark:
    cli: str = "lark-cli"
    identity: str = "user"
    confirm_base_token: str = ""
    confirm_table_id: str = ""
    report_folder_token: str = ""


@dataclass(frozen=True)
class Webui:
    source_dir: str
    modules_file: str
    modules_export: str


@dataclass(frozen=True)
class Target:
    base_url: str
    allowed_hosts: tuple[str, ...]
    login_path: str
    default_account: str = "default"


@dataclass(frozen=True)
class Account:
    login: str
    phone_env: str
    password_env: str


@dataclass(frozen=True)
class Budget:
    nav_hop_actions: int = 5
    nav_replans: int = 2
    step_actions_default: int = 15
    step_actions: Mapping[str, int] = field(default_factory=dict)
    case_minutes: int = 10
    env_repairs_per_case: int = 2


@dataclass(frozen=True)
class Thresholds:
    profile_fit: float = 0.7
    auto_confirm: float = 0.9
    arrive_wait_s: float = 5.0
    flaky_runs: int = 3


@dataclass(frozen=True)
class Paths:
    data_dir: str
    runs_dir: str


TRACE_MODES = ("off", "on_failure", "always")
LOGIN_MODES = ("phone_form", "none")


@dataclass(frozen=True)
class Browser:
    """浏览器会话（docs/agent-design.md 第 4、13 节）。profile_dir 是 QA 专用目录，不要指向日常 Chrome。"""

    profile_dir: str = "qa_state/profile"
    channel: str = "chrome"            # 本机 Chrome；空字符串用 Playwright 自带的 Chromium
    headless: bool = False
    viewport_width: int = 1440
    viewport_height: int = 900
    trace: str = "on_failure"


@dataclass(frozen=True)
class Session:
    module: str = "数据平台-新版"       # 健康检查要求的 selectedModule.name；空字符串不检查
    login: str = "phone_form"
    probe_api: str = "/api/v2/user/me"
    relogin_max: int = 1


@dataclass(frozen=True)
class AgentConfig:
    max_candidates: int = 30
    min_confidence: float = 0.5
    max_jev_calls: int = 2
    name_shortcut: bool = True
    promote_after: int = 3
    cache_path: str = "qa_state/decisions.json"


@dataclass(frozen=True)
class DataSource:
    """一类测试数据从哪里读（docs/agent-design.md 第 17 节）：某个页面的表格，按列取身份、状态、分组。"""

    page: str                  # 导航图节点 id：列表页
    id_column: str             # 身份列：同一条数据在不同时刻、不同页面都靠它认出来
    state_column: str = ""     # 状态列：按状态挑数据；空表示不区分状态
    group_column: str = ""     # 分组列（例如批次）：同组数据互相影响，用于耦合条件


@dataclass(frozen=True)
class Config:
    path: Path
    jev: Jev
    text_model: TextModel
    compile_llm: CompileLlm
    lark: Lark
    webui: Webui
    target: Target
    accounts: Mapping[str, Account]
    test_data: Mapping[str, str]
    budget: Budget
    thresholds: Thresholds
    paths: Paths
    browser: Browser = field(default_factory=Browser)
    session: Session = field(default_factory=Session)
    agent: AgentConfig = field(default_factory=AgentConfig)
    # requires 名 → 断言 DSL 列表：数据前置探针（docs/agent-design.md 6.2）
    probes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    data_sources: Mapping[str, DataSource] = field(default_factory=dict)

    def secret(self, env_name: str) -> str:
        value = os.environ.get(env_name, "")
        if not value:
            raise ConfigError(f"环境变量 {env_name} 未设置：请写入 .env")
        return value


def _section(data: Mapping[str, Any], name: str, source: Path) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        raise ConfigError(f"{source} 缺少 [{name}] 分节，可在 qa.example.toml 里找到写法")
    if not isinstance(value, dict):
        raise ConfigError(f"{source} 的 [{name}] 必须是表，实际是 {type(value).__name__}")
    return value


def _required(section: Mapping[str, Any], name: str, key: str, source: Path) -> Any:
    if key not in section:
        raise ConfigError(f"{source} 缺少 [{name}].{key}，可在 qa.example.toml 里找到写法")
    return section[key]


def _text(section: Mapping[str, Any], name: str, key: str, source: Path) -> str:
    value = _required(section, name, key, source)
    if not isinstance(value, str):
        raise ConfigError(f"{source} 的 [{name}].{key} 必须是字符串，实际是 {type(value).__name__}")
    return value


def _texts(section: Mapping[str, Any], name: str, key: str, source: Path) -> tuple[str, ...]:
    value = _required(section, name, key, source)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{source} 的 [{name}].{key} 必须是字符串列表")
    return tuple(value)


def _number(section: Mapping[str, Any], name: str, key: str, source: Path, default: Any) -> Any:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{source} 的 [{name}].{key} 必须是数字，实际是 {type(value).__name__}")
    return value


def _optional(data: Mapping[str, Any], name: str, source: Path) -> dict[str, Any]:
    """可选分节：不存在时返回空表，全部取默认值。"""
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{source} 的 [{name}] 必须是表，实际是 {type(value).__name__}")
    return value


def _flag(section: Mapping[str, Any], name: str, key: str, source: Path, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{source} 的 [{name}].{key} 必须是 true / false，实际是 {type(value).__name__}")
    return value


def _choice(
    section: Mapping[str, Any], name: str, key: str, source: Path, default: str, choices: Sequence[str]
) -> str:
    value = section.get(key, default)
    if value not in choices:
        raise ConfigError(f"{source} 的 [{name}].{key} 只能是 {' / '.join(choices)}，实际是 {value!r}")
    return str(value)


def _browser(data: Mapping[str, Any], source: Path) -> Browser:
    section = _optional(data, "browser", source)
    viewport = section.get("viewport", {})
    if not isinstance(viewport, dict):
        raise ConfigError(f"{source} 的 [browser].viewport 必须是表，例如 {{ width = 1440, height = 900 }}")
    return Browser(
        profile_dir=str(section.get("profile_dir", Browser.profile_dir)),
        channel=str(section.get("channel", Browser.channel)),
        headless=_flag(section, "browser", "headless", source, Browser.headless),
        viewport_width=int(_number(viewport, "browser.viewport", "width", source, Browser.viewport_width)),
        viewport_height=int(_number(viewport, "browser.viewport", "height", source, Browser.viewport_height)),
        trace=_choice(section, "browser", "trace", source, Browser.trace, TRACE_MODES),
    )


def _session(data: Mapping[str, Any], source: Path) -> Session:
    section = _optional(data, "session", source)
    return Session(
        module=str(section.get("module", Session.module)),
        login=_choice(section, "session", "login", source, Session.login, LOGIN_MODES),
        probe_api=str(section.get("probe_api", Session.probe_api)),
        relogin_max=int(_number(section, "session", "relogin_max", source, Session.relogin_max)),
    )


def _agent(data: Mapping[str, Any], source: Path) -> AgentConfig:
    section = _optional(data, "agent", source)
    defaults = AgentConfig()
    return AgentConfig(
        max_candidates=int(_number(section, "agent", "max_candidates", source, defaults.max_candidates)),
        min_confidence=float(_number(section, "agent", "min_confidence", source, defaults.min_confidence)),
        max_jev_calls=int(_number(section, "agent", "max_jev_calls", source, defaults.max_jev_calls)),
        name_shortcut=_flag(section, "agent", "name_shortcut", source, defaults.name_shortcut),
        promote_after=int(_number(section, "agent", "promote_after", source, defaults.promote_after)),
        cache_path=str(section.get("cache_path", defaults.cache_path)),
    )


def _probes(data: Mapping[str, Any], source: Path) -> dict[str, tuple[str, ...]]:
    section = _optional(data, "probes", source)
    return {str(name): _texts(section, "probes", name, source) for name in section}


def _data_sources(data: Mapping[str, Any], source: Path) -> dict[str, DataSource]:
    section = _optional(data, "data_sources", source)
    found: dict[str, DataSource] = {}
    for name, item in section.items():
        where = f"data_sources.{name}"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{source} 的 [{where}] 必须是表")
        found[str(name)] = DataSource(
            page=_text(item, where, "page", source),
            id_column=_text(item, where, "id_column", source),
            state_column=str(item.get("state_column", "")),
            group_column=str(item.get("group_column", "")),
        )
    return found


def _apply_overrides(data: dict[str, Any], overrides: Mapping[str, Any], source: Path) -> None:
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        node: Any = data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ConfigError(f"命令行参数 {dotted} 指向的 [{parts[0]}] 在 {source} 里不存在")
            node = node[part]
        if not isinstance(node, dict):
            raise ConfigError(f"命令行参数 {dotted} 无法写入 {source}")
        node[parts[-1]] = value


def _accounts(data: Mapping[str, Any], source: Path) -> dict[str, Account]:
    section = _section(data, "accounts", source)
    if not section:
        raise ConfigError(f"{source} 的 [accounts] 至少要有一个账号，例如 [accounts.default]")
    accounts: dict[str, Account] = {}
    for name, body in section.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{source} 的 [accounts.{name}] 必须是表")
        accounts[name] = Account(
            login=_text(body, f"accounts.{name}", "login", source),
            phone_env=_text(body, f"accounts.{name}", "phone_env", source),
            password_env=_text(body, f"accounts.{name}", "password_env", source),
        )
    return accounts


def load_config(path: Path | None = None, overrides: Mapping[str, Any] | None = None) -> Config:
    source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not source.exists():
        raise ConfigError(f"找不到配置文件 {source}：请复制 qa.example.toml 为 qa.toml 后填写")
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    if overrides:
        _apply_overrides(data, overrides, source)

    jev = _section(data, "jev", source)
    text_model = _section(data, "text_model", source)
    compile_llm = _section(data, "compile_llm", source)
    lark = _section(data, "lark", source)
    webui = _section(data, "webui", source)
    target = _section(data, "target", source)
    budget = _section(data, "budget", source)
    thresholds = _section(data, "thresholds", source)
    paths = _section(data, "paths", source)

    test_data = data.get("test_data", {})
    if not isinstance(test_data, dict):
        raise ConfigError(f"{source} 的 [test_data] 必须是表")

    return Config(
        path=source,
        jev=Jev(
            base_url=_text(jev, "jev", "base_url", source),
            model=_text(jev, "jev", "model", source),
            api_key_env=str(jev.get("api_key_env", "TYPESAFE_API_KEY")),
        ),
        text_model=TextModel(
            base_url=_text(text_model, "text_model", "base_url", source),
            model=_text(text_model, "text_model", "model", source),
            api_key_env=str(text_model.get("api_key_env", "TEXT_MODEL_API_KEY")),
            reasoning=str(text_model.get("reasoning", "")),
        ),
        compile_llm=CompileLlm(
            base_url=_text(compile_llm, "compile_llm", "base_url", source),
            model=_text(compile_llm, "compile_llm", "model", source),
            api_key_env=str(compile_llm.get("api_key_env", "TEXT_MODEL_API_KEY")),
            temperature=float(_number(compile_llm, "compile_llm", "temperature", source, 0.0)),
            timeout_s=int(_number(compile_llm, "compile_llm", "timeout_s", source, 60)),
            prompt_version=str(compile_llm.get("prompt_version", "v1")),
        ),
        lark=Lark(
            cli=str(lark.get("cli", "lark-cli")),
            identity=str(lark.get("identity", "user")),
            confirm_base_token=str(lark.get("confirm_base_token", "")),
            confirm_table_id=str(lark.get("confirm_table_id", "")),
            report_folder_token=str(lark.get("report_folder_token", "")),
        ),
        webui=Webui(
            source_dir=_text(webui, "webui", "source_dir", source),
            modules_file=_text(webui, "webui", "modules_file", source),
            modules_export=_text(webui, "webui", "modules_export", source),
        ),
        target=Target(
            base_url=_text(target, "target", "base_url", source),
            allowed_hosts=_texts(target, "target", "allowed_hosts", source),
            login_path=_text(target, "target", "login_path", source),
            default_account=str(target.get("default_account", "default")),
        ),
        accounts=_accounts(data, source),
        test_data={str(key): str(value) for key, value in test_data.items()},
        budget=Budget(
            nav_hop_actions=int(_number(budget, "budget", "nav_hop_actions", source, 5)),
            nav_replans=int(_number(budget, "budget", "nav_replans", source, 2)),
            step_actions_default=int(_number(budget, "budget", "step_actions_default", source, 15)),
            step_actions={
                str(key): int(value) for key, value in budget.get("step_actions", {}).items()
            },
            case_minutes=int(_number(budget, "budget", "case_minutes", source, 10)),
            env_repairs_per_case=int(_number(budget, "budget", "env_repairs_per_case", source, 2)),
        ),
        thresholds=Thresholds(
            profile_fit=float(_number(thresholds, "thresholds", "profile_fit", source, 0.7)),
            auto_confirm=float(_number(thresholds, "thresholds", "auto_confirm", source, 0.9)),
            arrive_wait_s=float(_number(thresholds, "thresholds", "arrive_wait_s", source, 5)),
            flaky_runs=int(_number(thresholds, "thresholds", "flaky_runs", source, 3)),
        ),
        paths=Paths(
            data_dir=_text(paths, "paths", "data_dir", source),
            runs_dir=_text(paths, "paths", "runs_dir", source),
        ),
        browser=_browser(data, source),
        session=_session(data, source),
        agent=_agent(data, source),
        probes=_probes(data, source),
        data_sources=_data_sources(data, source),
    )

