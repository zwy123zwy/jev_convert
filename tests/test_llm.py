import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jev_convert.compile.llm import Llm
from jev_convert.config import Config, load_config
from jev_convert.jsonio import load

TEMPLATE = Path(__file__).resolve().parent.parent / "qa.example.toml"
CANDIDATES = ["开始处理", "关闭工作台", "任务池筛选"]
VALID_PROFILE = {
    "case_depth": 2,
    "module_depths": [1],
    "step_marker": "→",
    "expect_marker": "预期",
    "precondition_keywords": ["前置"],
    "separators": ["→", "，"],
    "scope": "case",
}


def make_config(tmp_path: Path, prompt_version: str = "v1") -> Config:
    target = tmp_path / "qa.toml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return load_config(target, overrides={"compile_llm.prompt_version": prompt_version})


class FakeTransport:
    """假 transport：按调用次序返回预置回答，并记录每个请求体。"""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(body)
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        content = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


def broken(body: Mapping[str, Any]) -> Mapping[str, Any]:
    raise AssertionError("缓存命中时不应该调用 transport")


def prompt_text(body: Mapping[str, Any]) -> str:
    return "\n".join(message["content"] for message in body["messages"])


def test_choose_returns_index_and_reason(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 2, "reason": "最接近开始处理"})
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    choice = llm.choose("选择点击目标", {"page": "任务池"}, CANDIDATES)

    assert choice.index == 1
    assert choice.reason == "最接近开始处理"
    assert choice.unresolved is False
    assert len(transport.calls) == 1


def test_request_body_is_openai_json_mode(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    transport = FakeTransport({"choice": 1, "reason": "ok"})
    Llm(config, tmp_path / "cache", transport).choose("选择点击目标", {}, CANDIDATES)

    body = transport.calls[0]
    assert body["model"] == config.compile_llm.model
    assert body["temperature"] == config.compile_llm.temperature
    assert body["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]


def test_prompt_lists_numbered_candidates(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 1, "reason": "ok"})
    Llm(make_config(tmp_path), tmp_path / "cache", transport).choose("选择点击目标", {"page": "任务池"}, CANDIDATES)

    text = prompt_text(transport.calls[0])
    assert "1. 开始处理" in text
    assert "3. 任务池筛选" in text
    assert '"choice"' in text


def test_out_of_range_retries_once_then_unresolved(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 9, "reason": "越界"}, {"choice": 0, "reason": "越界"})
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    choice = llm.choose("选择点击目标", {}, CANDIDATES)

    assert choice.index is None
    assert choice.unresolved is True
    assert "越界" in choice.reason
    assert len(transport.calls) == 2
    assert not list((tmp_path / "cache").glob("*/*.json"))


def test_invalid_json_retries_once_then_accepts(tmp_path: Path) -> None:
    transport = FakeTransport("这不是 JSON", {"choice": 3, "reason": "第三个"})
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    choice = llm.choose("选择点击目标", {}, CANDIDATES)

    assert choice.index == 2
    assert choice.unresolved is False
    assert len(transport.calls) == 2


def test_invalid_json_twice_is_unresolved(tmp_path: Path) -> None:
    transport = FakeTransport("不是 JSON", "[1, 2]")
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    choice = llm.choose("选择点击目标", {}, CANDIDATES)

    assert choice.unresolved is True
    assert choice.index is None
    assert len(transport.calls) == 2


def test_second_call_hits_cache(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 2, "reason": "最接近"})
    cache = tmp_path / "cache"
    llm = Llm(make_config(tmp_path), cache, transport)

    first = llm.choose("选择点击目标", {"page": "任务池"}, CANDIDATES)
    second = llm.choose("选择点击目标", {"page": "任务池"}, CANDIDATES)

    assert first == second
    assert len(transport.calls) == 1


def test_context_key_order_does_not_change_key(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 1, "reason": "第一个"})
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    llm.choose("选择点击目标", {"page": "任务池", "count": 2}, CANDIDATES)
    llm.choose("选择点击目标", {"count": 2, "page": "任务池"}, CANDIDATES)

    assert len(transport.calls) == 1


def test_cache_file_path_and_content(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    transport = FakeTransport({"choice": 2, "reason": "最接近"})
    Llm(make_config(tmp_path), cache, transport).choose("选择点击目标", {}, CANDIDATES)

    files = sorted(cache.glob("*/*.json"))
    assert len(files) == 1
    path = files[0]
    assert len(path.stem) == 64
    assert path.parent.name == path.stem[:2]
    assert load(path) == {"choice": 2, "reason": "最接近"}


def test_prompt_version_change_invalidates_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    transport = FakeTransport({"choice": 1, "reason": "v1"}, {"choice": 3, "reason": "v2"})

    first = Llm(make_config(tmp_path, "v1"), cache, transport).choose("选择点击目标", {}, CANDIDATES)
    second = Llm(make_config(tmp_path, "v2"), cache, transport).choose("选择点击目标", {}, CANDIDATES)

    assert first.reason == "v1"
    assert second.reason == "v2"
    assert len(transport.calls) == 2
    assert len(list(cache.glob("*/*.json"))) == 2


def test_cache_hit_does_not_reach_transport(tmp_path: Path, caplog: Any) -> None:
    cache = tmp_path / "cache"
    llm = Llm(make_config(tmp_path), cache, FakeTransport({"choice": 1, "reason": "第一个"}))
    llm.choose("选择点击目标", {}, CANDIDATES)

    cached = Llm(make_config(tmp_path), cache, broken)
    with caplog.at_level(logging.DEBUG, logger="jev_convert.compile.llm"):
        choice = cached.choose("选择点击目标", {}, CANDIDATES)

    assert choice.index == 0
    assert "cache=hit" in caplog.text


def test_corrupt_cache_file_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    transport = FakeTransport({"choice": 1, "reason": "重新请求"})
    llm = Llm(make_config(tmp_path), cache, transport)
    llm.choose("选择点击目标", {}, CANDIDATES)

    path = next(cache.glob("*/*.json"))
    path.write_text("{ 坏掉的 JSON", encoding="utf-8")
    choice = llm.choose("选择点击目标", {}, CANDIDATES)

    assert choice.index == 0
    assert len(transport.calls) == 2


def test_choose_without_candidates_skips_transport(tmp_path: Path) -> None:
    transport = FakeTransport({"choice": 1, "reason": "ok"})
    choice = Llm(make_config(tmp_path), tmp_path / "cache", transport).choose("选择点击目标", {}, [])

    assert choice.unresolved is True
    assert choice.index is None
    assert transport.calls == []


def test_propose_profiles_keeps_only_valid_items(tmp_path: Path) -> None:
    invalid_number = {**VALID_PROFILE, "case_depth": "2"}
    missing_field = {"case_depth": 2, "module_depths": []}
    transport = FakeTransport({"profiles": [VALID_PROFILE, invalid_number, missing_field]})
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    profiles = llm.propose_profiles(["数据平台-新版\n  工作台\n    标注工作台"])

    assert profiles == [VALID_PROFILE]
    assert len(transport.calls) == 1
    assert transport.calls[0]["response_format"] == {"type": "json_object"}


def test_propose_profiles_is_cached(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    transport = FakeTransport({"profiles": [VALID_PROFILE]})
    llm = Llm(make_config(tmp_path), cache, transport)

    first = llm.propose_profiles(["子树"])
    second = llm.propose_profiles(["子树"])

    assert first == second == [VALID_PROFILE]
    assert len(transport.calls) == 1
    assert len(list(cache.glob("*/*.json"))) == 1


def test_propose_profiles_returns_empty_on_bad_answer(tmp_path: Path) -> None:
    transport = FakeTransport("不是 JSON")
    llm = Llm(make_config(tmp_path), tmp_path / "cache", transport)

    assert llm.propose_profiles(["子树"]) == []
    assert not list((tmp_path / "cache").glob("*/*.json"))

def test_without_api_key_stays_rule_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    config = make_config(tmp_path)
    llm = Llm(config, tmp_path / "cache")
    assert llm.available is False
    choice = llm.choose("拆分步骤", {"text": "点击开始处理"}, CANDIDATES)
    assert choice.unresolved is True
    assert "TEXT_MODEL_API_KEY" in choice.reason
    assert llm.propose_profiles(["样例"]) == []
    assert not (tmp_path / "cache").exists()
