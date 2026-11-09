"""编译期 LLM 调用（第 4.4 节）：候选选择、写法画像推断、可复现缓存。

LLM 只做选择：请求里给出带编号的候选，要求返回固定 JSON，代码校验取值属于候选集合。
所有调用走同一条链路：请求体由本模块拼、HTTP 层由 transport 注入（测试用假 transport 替换）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from jev_convert import jsonio
from jev_convert.config import Config

logger = logging.getLogger(__name__)

ATTEMPTS = 2  # 首次 + 重试 1 次
PROFILE_FIELDS = (
    "case_depth",
    "module_depths",
    "step_marker",
    "expect_marker",
    "precondition_keywords",
    "separators",
    "scope",
)
PROFILE_TASK = "propose_profiles"

_SYSTEM = "你是测试用例编译助手。只输出 JSON 对象，不要输出解释、不要用代码块包裹。"
_CHOICE_TEMPLATE = """任务：{task}

上下文（JSON）：
{context}

候选（编号从 1 开始）：
{candidates}

只返回 JSON：{{"choice": <上面列出的候选编号>, "reason": "<一句话理由>"}}"""
_PROFILE_TEMPLATE = """下面是若干棵用例思维导图的子树，用缩进行表示层级：

{samples}

归纳它们的写法画像。只返回 JSON：
{{"profiles": [{{"case_depth": <整数>, "module_depths": [<整数>], "step_marker": "<字符串>",
  "expect_marker": "<字符串>", "precondition_keywords": ["<字符串>"], "separators": ["<字符串>"],
  "scope": "<字符串>"}}]}}"""


@dataclass(frozen=True)
class Choice:
    index: int | None
    reason: str
    unresolved: bool = False


Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _http_transport(config: Config) -> Transport:
    """默认 transport：OpenAI 兼容的 /chat/completions，密钥在使用时才读取。"""

    def send(body: Mapping[str, Any]) -> Mapping[str, Any]:
        import httpx

        url = config.compile_llm.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.secret(config.compile_llm.api_key_env)}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=config.compile_llm.timeout_s) as client:
            response = client.post(url, json=dict(body), headers=headers)
            response.raise_for_status()
            return response.json()

    return send


def _decode(response: Mapping[str, Any]) -> Mapping[str, Any]:
    """从 chat/completions 响应里取出模型返回的 JSON 对象。"""
    choices = response["choices"]
    message = choices[0]["message"]
    payload = json.loads(message["content"])
    if not isinstance(payload, Mapping):
        raise ValueError(f"响应不是 JSON 对象：{type(payload).__name__}")
    return payload


def _valid_profiles(items: Any) -> list[dict[str, Any]]:
    """只保留字段齐全、类型正确的画像；其他丢弃。"""
    if not isinstance(items, (list, tuple)):
        return []
    profiles: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != set(PROFILE_FIELDS):
            continue
        case_depth = item["case_depth"]
        if isinstance(case_depth, bool) or not isinstance(case_depth, int) or case_depth < 0:
            continue
        depths = item["module_depths"]
        if not isinstance(depths, (list, tuple)) or not all(
            not isinstance(depth, bool) and isinstance(depth, int) and depth >= 0 for depth in depths
        ):
            continue
        step_marker = item["step_marker"]
        expect_marker = item["expect_marker"]
        scope = item["scope"]
        if not all(isinstance(value, str) for value in (step_marker, expect_marker, scope)):
            continue
        keywords = item["precondition_keywords"]
        separators = item["separators"]
        if not all(
            isinstance(value, (list, tuple)) and all(isinstance(word, str) for word in value)
            for value in (keywords, separators)
        ):
            continue
        profiles.append(
            {
                "case_depth": case_depth,
                "module_depths": [int(depth) for depth in depths],
                "step_marker": step_marker,
                "expect_marker": expect_marker,
                "precondition_keywords": [str(word) for word in keywords],
                "separators": [str(word) for word in separators],
                "scope": scope,
            }
        )
    return profiles


class Llm:
    """编译期 LLM 客户端：缓存目录 + 可注入 transport。"""

    def __init__(self, config: Config, cache_dir: Path, transport: Transport | None = None) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.transport = transport if transport is not None else _http_transport(config)
        name = config.compile_llm.api_key_env
        self.missing_key = "" if transport is not None or os.environ.get(name, "") else name
        self.hits = 0          # 本次编译命中缓存的次数
        self.misses = 0        # 本次编译真正发出请求的次数

    @property
    def available(self) -> bool:
        """没有密钥时整条链路退化成规则模式：每次都 unresolved，由调用方转人工确认。"""
        return not self.missing_key

    def choose(self, task: str, context: Mapping[str, Any], candidates: Sequence[str]) -> Choice:
        items = tuple(str(item) for item in candidates)
        if self.missing_key:
            return Choice(index=None, reason=f"环境变量 {self.missing_key} 未设置，跳过模型选择", unresolved=True)
        if not items:
            return Choice(index=None, reason="没有候选可选项", unresolved=True)
        key = self._cache_key(task, context, items)
        body = self._body(self._choice_prompt(task, context, items))
        reason = "模型没有返回合法选择"
        for _ in range(ATTEMPTS):
            answer, hit = self._ask(key, body)
            if answer is None:
                continue
            index, reason = self._checked_choice(answer, len(items))
            if index is None:
                continue
            if not hit:
                self._remember(key, {"choice": index + 1, "reason": reason})
            return Choice(index=index, reason=reason)
        return Choice(index=None, reason=reason, unresolved=True)

    def propose_profiles(self, samples: Sequence[str]) -> list[dict[str, Any]]:
        if self.missing_key:
            return []
        texts = tuple(str(sample) for sample in samples)
        key = self._cache_key(PROFILE_TASK, {"samples": texts}, ())
        answer, hit = self._ask(key, self._body(self._profile_prompt(texts)))
        if answer is None:
            return []
        profiles = _valid_profiles(answer.get("profiles"))
        if not profiles:
            return []
        if not hit:
            self._remember(key, {"profiles": profiles})
        return profiles

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.config.compile_llm.model,
            "temperature": self.config.compile_llm.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }

    def _choice_prompt(self, task: str, context: Mapping[str, Any], items: tuple[str, ...]) -> str:
        listed = "\n".join(f"{position}. {item}" for position, item in enumerate(items, start=1))
        shown = json.dumps(context, ensure_ascii=False, indent=2, default=str)
        return _CHOICE_TEMPLATE.format(task=task, context=shown, candidates=listed)

    def _profile_prompt(self, samples: tuple[str, ...]) -> str:
        return _PROFILE_TEMPLATE.format(samples="\n".join(samples))

    def _checked_choice(self, answer: Mapping[str, Any], count: int) -> tuple[int | None, str]:
        value = answer.get("choice")
        reason = answer.get("reason")
        text = reason if isinstance(reason, str) and reason.strip() else "模型没有给出理由"
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= count:
            return None, f"编号越界：{value!r}，候选编号是 1–{count}"
        return value - 1, text

    def _cache_key(self, task: str, context: Mapping[str, Any], candidates: Sequence[str]) -> str:
        payload = {
            "task": task,
            "prompt_version": self.config.compile_llm.prompt_version,
            "model": self.config.compile_llm.model,
            "context": _canonical(context),
            "candidates": list(candidates),
        }
        return sha256(_canonical(payload).encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _ask(self, key: str, body: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool]:
        path = self._cache_path(key)
        if path.exists():
            try:
                cached = jsonio.load(path)
            except (OSError, ValueError):
                cached = None
            if isinstance(cached, Mapping):
                logger.debug("llm cache=hit key=%s", key[:12])
                self.hits += 1
                return cached, True
        logger.debug("llm cache=miss key=%s", key[:12])
        self.misses += 1
        try:
            return _decode(self.transport(body)), False
        except (KeyError, IndexError, TypeError, ValueError) as error:
            logger.debug("llm response rejected key=%s: %s", key[:12], error)
            return None, False

    def _remember(self, key: str, answer: Mapping[str, Any]) -> None:
        jsonio.dump(dict(answer), self._cache_path(key))


STATS_FILE = "llm-stats.json"


def cache_entries(cache_dir: Path) -> int:
    """缓存条目数：<cache_dir>/<前两位>/<哈希>.json。"""
    folder = Path(cache_dir)
    return sum(1 for _ in folder.glob("*/*.json")) if folder.is_dir() else 0


def write_stats(path: Path, hits: int, misses: int) -> dict[str, Any]:
    """记下最近一次编译的命中情况。放在运行目录（不入 git），不写进 qa_data。"""
    total = hits + misses
    stats = {"hits": hits, "misses": misses, "hit_rate": round(hits / total, 4) if total else None}
    jsonio.dump({"last_compile": stats}, Path(path))
    return stats
