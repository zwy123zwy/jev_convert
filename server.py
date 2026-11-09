"""jev-convert 本地可视化服务：编译导图、浏览计划库、触发确认读回。

只用标准库（http.server）。所有耗时操作（compile / confirm / kb build）作为后台任务串行执行，
前端轮询 /api/task/<id> 看日志。启动：uv run python server.py [--port 8765]。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jev_convert import jsonio
from jev_convert.cli import _project_path, load_environment  # noqa: I001 - 与模块导入分组见下
from jev_convert.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend" / "dist"     # React 构建产物（npm run build）；开发模式用 vite dev + /api 代理

_BUILD_HINT = ("前端尚未构建：cd frontend && npm install && npm run build；"
               "开发模式可用 npm run dev（/api 自动代理到本服务）")

_TASKS: dict[str, dict[str, object]] = {}
_TASK_SEQ = 0
_TASK_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()      # 编译与确认都写计划库，串行执行


def _data_dir() -> Path:
    load_environment()
    config = load_config()
    return _project_path(config.paths.data_dir)


def deps() -> dict[str, object]:
    """前置依赖清单：执行（kb build / compile / confirm）之前必须就绪的内容。"""
    import shutil

    load_environment()
    try:
        config = load_config()
        config_error = ""
    except ConfigError as error:
        config = None
        config_error = str(error)
    items: list[dict[str, object]] = [{
        "id": "config", "label": "qa.toml 配置文件",
        "ready": config is not None,
        "hint": config_error or "已加载",
    }]
    if config is None:
        for label in ("webui 源码目录", "Node 可执行文件", "kb/extract 依赖（npm install）",
                      "编译期 LLM key", "知识库已构建", "lark-cli 可用", "确认表配置"):
            items.append({"id": "pending", "label": label, "ready": None,
                          "hint": "先修好 qa.toml 再检查"})
        return {"items": items}

    extract_dir = ROOT / "jev_convert" / "kb" / "extract"
    webui = Path(config.webui.source_dir).expanduser()
    kb_dir = _project_path(config.paths.data_dir) / "kb"
    kb_version = None
    try:
        kb_version = jsonio.load(kb_dir / "navgraph.json").get("kb_version")
    except (OSError, ValueError):
        pass
    llm_env = config.compile_llm.api_key_env
    table = config.lark.confirm_base_token and config.lark.confirm_table_id
    items += [
        {"id": "webui", "label": "webui 源码目录",
         "ready": webui.is_dir(),
         "hint": str(webui) if webui.is_dir() else f"目录不存在：{webui}（kb build 抽取知识库用）"},
        {"id": "node", "label": "Node 可执行文件",
         "ready": shutil.which("node") is not None,
         "hint": "已安装" if shutil.which("node") else "PATH 里找不到 node（kb build 用）"},
        {"id": "node_modules", "label": "kb/extract 依赖（npm install）",
         "ready": (extract_dir / "node_modules").is_dir(),
         "hint": "已安装" if (extract_dir / "node_modules").is_dir()
                 else "先执行 npm install --prefix jev_convert/kb/extract"},
        {"id": "llm", "label": f"编译期 LLM key（{llm_env}）",
         "ready": bool(os.environ.get(llm_env, "")),
         "hint": "已设置" if os.environ.get(llm_env, "") else "写入 .env 后重启服务生效"},
        {"id": "kb", "label": "知识库已构建",
         "ready": kb_version is not None,
         "hint": f"kb_version={kb_version}" if kb_version else "qa_data/kb/navgraph.json 不存在：先运行 kb build"},
        {"id": "lark", "label": "lark-cli 可用",
         "ready": shutil.which(config.lark.cli) is not None,
         "hint": "已安装" if shutil.which(config.lark.cli)
                 else f"PATH 里找不到 {config.lark.cli}（读导图、读写确认表用）"},
        {"id": "table", "label": "确认表配置",
         "ready": bool(table), "optional": True,
         "hint": "已配置" if table
                 else "可选：[lark].confirm_base_token / confirm_table_id 未配置时导出与读回会跳过"},
    ]
    return {"items": items}


def overview() -> dict[str, object]:
    """首页统计：知识库版本、各导图的计划状态计数、llm 缓存与命中率。"""
    from jev_convert.compile.llm import cache_entries
    from jev_convert.compile.pipeline import LLM_CACHE_DIR

    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        return {"error": str(error)}
    data_dir = _project_path(config.paths.data_dir)
    kb_version = None
    try:
        kb_version = jsonio.load(data_dir / "kb" / "navgraph.json").get("kb_version")
    except (OSError, ValueError):
        pass
    plans: list[dict[str, object]] = []
    plans_root = data_dir / "plans"
    if plans_root.is_dir():
        for folder in sorted(plans_root.iterdir()):
            index_path = folder / "index.json"
            if not folder.is_dir() or not index_path.exists():
                continue
            try:
                index = jsonio.load(index_path)
            except (OSError, ValueError):
                continue
            cases = index.get("cases", {}) if isinstance(index, dict) else {}
            counts: dict[str, int] = {}
            for item in cases.values():
                state = str(item.get("status", "draft"))
                counts[state] = counts.get(state, 0) + 1
            plans.append({"token": folder.name, "total": len(cases), "counts": counts})
    stats_path = _project_path(config.paths.runs_dir) / "llm-stats.json"
    llm: dict[str, object] = {"cache_entries": cache_entries(data_dir / LLM_CACHE_DIR)}
    if stats_path.exists():
        try:
            llm.update(jsonio.load(stats_path).get("last_compile", {}))
        except (OSError, ValueError):
            pass
    return {"kb_version": kb_version, "plans": plans, "llm": llm}


def plan_index(token: str) -> dict[str, object]:
    index = jsonio.load(_data_dir() / "plans" / token / "index.json")
    return index.get("cases", {}) if isinstance(index, dict) else {}


def plan_detail(token: str, case_id: str) -> dict[str, object]:
    cases = plan_index(token)
    item = cases.get(case_id)
    if not isinstance(item, dict):
        raise KeyError(case_id)
    return jsonio.load(_data_dir() / "plans" / token / str(item.get("file", f"{case_id}.json")))


def _run_task(kind: str, args: list[str]) -> str:
    global _TASK_SEQ
    with _TASK_LOCK:
        _TASK_SEQ += 1
        task_id = f"{time.strftime('%H%M%S')}-{_TASK_SEQ}"
        _TASKS[task_id] = {"kind": kind, "status": "running", "exit": None, "log": "",
                           "started": time.strftime("%H:%M:%S")}

    def work() -> None:
        from jev_convert import cli

        out, err = io.StringIO(), io.StringIO()
        try:
            with _RUN_LOCK, redirect_stdout(out), redirect_stderr(err):
                code = cli.main(args)
        except Exception as error:  # 任务里任何异常都进日志，不让线程死掉
            print(f"未预期的异常：{type(error).__name__}: {error}", file=err)
            code = 1
        with _TASK_LOCK:
            task = _TASKS[task_id]
            task["log"] = out.getvalue() + err.getvalue()
            task["exit"] = code
            task["status"] = "done" if code == 0 else "failed"

    threading.Thread(target=work, daemon=True).start()
    return task_id


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 基类签名
        pass          # 安静模式：访问日志不打到终端

    # ---------- 响应工具 ----------
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _static(self, name: str, content_type: str) -> None:
        path = FRONTEND / name
        if not path.is_file():
            body = _BUILD_HINT.encode("utf-8")
            self._send(503, body, "text/plain; charset=utf-8")
            return
        self._send(200, path.read_bytes(), content_type)

    def _body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        route = parts.path
        if route == "/":
            self._static("index.html", "text/html; charset=utf-8")
        elif route.startswith("/assets/"):
            name = route[len("/assets/"):]
            if not name or "/" in name or ".." in name:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            content_type = ("text/javascript; charset=utf-8" if name.endswith(".js")
                            else "text/css; charset=utf-8" if name.endswith(".css")
                            else "image/svg+xml" if name.endswith(".svg")
                            else "application/octet-stream")
            self._static(f"assets/{name}", content_type)
        elif route == "/api/overview":
            self._json(overview())
        elif route == "/api/deps":
            self._json(deps())
        elif route == "/api/plans":
            token = (query.get("token") or [""])[0]
            if not token:
                self._json({"error": "缺少 token"}, 400)
                return
            try:
                self._json(plan_index(token))
            except ConfigError as error:
                self._json({"error": str(error)}, 400)
            except (OSError, ValueError) as error:
                self._json({"error": str(error)}, 404)
        elif route == "/api/plan":
            token = (query.get("token") or [""])[0]
            case = (query.get("case") or [""])[0]
            if not token or not case:
                self._json({"error": "缺少 token 或 case"}, 400)
                return
            try:
                self._json(plan_detail(token, case))
            except ConfigError as error:
                self._json({"error": str(error)}, 400)
            except (KeyError, OSError, ValueError) as error:
                self._json({"error": str(error)}, 404)
        elif route.startswith("/api/task/"):
            task = _TASKS.get(route.rsplit("/", 1)[-1])
            self._json(task if task is not None else {"error": "任务不存在"}, 200 if task else 404)
        elif route == "/api/tasks":
            with _TASK_LOCK:
                items = [{"id": key, **{name: value for name, value in task.items() if name != "log"}}
                         for key, task in sorted(_TASKS.items(), reverse=True)]
            self._json(items)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        route = urlsplit(self.path).path
        body = self._body()
        if route == "/api/compile":
            url = str(body.get("url", "")).strip()
            until = str(body.get("until", "s6"))
            if not url:
                self._json({"error": "缺少导图链接"}, 400)
                return
            self._json({"task": _run_task("compile", ["compile", url, "--until", until])})
        elif route == "/api/confirm":
            url = str(body.get("url", "")).strip()
            if not url:
                self._json({"error": "缺少导图链接"}, 400)
                return
            self._json({"task": _run_task("confirm", ["confirm", url])})
        elif route == "/api/kb-build":
            webui = str(body.get("webui", "")).strip()
            self._json({"task": _run_task("kb-build", ["kb", "build", "--webui", webui] if webui
                                          else ["kb", "build"])})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="jev-convert 本地可视化服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"jev-convert 可视化服务：http://{args.host}:{args.port}  （Ctrl-C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
