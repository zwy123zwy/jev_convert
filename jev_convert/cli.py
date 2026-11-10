"""jev-convert 命令行入口：config check、kb build、compile（结束时导出确认表）、confirm、llm stats。

只包含「测试用例转化 agent」的链路：导图 → 编译 → 计划库 → 人工确认。
浏览器执行（jev-qa 的 run / learn）不在这个项目里。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jev_convert import jsonio
from jev_convert.compile.pipeline import CompileResult, compile_mindnote
from jev_convert.compile.s0_read import AuthRequired
from jev_convert.config import PROJECT_ROOT, Config, ConfigError, load_config, load_environment
from jev_convert.kb.load import KbLoadError, load_graph
from jev_convert.lark import LarkError, MissingScope

COMPILE_STAGES = ("s0", "s1", "s2", "s3", "s4", "s5", "s6")


def _configured(label: str, value: str) -> tuple[str, str]:
    return label, "已配置" if value else "缺失"


def _items(config: Config) -> list[tuple[str, str]]:
    rows = [
        _configured("[jev].base_url", config.jev.base_url),
        _configured("[jev].model", config.jev.model),
        _configured("[text_model].base_url", config.text_model.base_url),
        _configured("[text_model].model", config.text_model.model),
        _configured("[compile_llm].base_url", config.compile_llm.base_url),
        _configured("[compile_llm].model", config.compile_llm.model),
        _configured("[webui].source_dir", config.webui.source_dir),
        _configured("[target].base_url", config.target.base_url),
        _configured("[lark].confirm_base_token", config.lark.confirm_base_token),
        _configured("[lark].confirm_table_id", config.lark.confirm_table_id),
        _configured("[paths].data_dir", config.paths.data_dir),
        _configured("[paths].runs_dir", config.paths.runs_dir),
    ]
    for name in sorted(config.test_data):
        rows.append(_configured(f"[test_data].{name}", config.test_data[name]))
    for name in sorted(config.accounts):
        account = config.accounts[name]
        rows.append(_configured(f"[accounts.{name}].phone_env", account.phone_env))
        rows.append(_configured(f"[accounts.{name}].password_env", account.password_env))
    return rows


def _config_check() -> int:
    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2

    print(f"配置：{config.path}")
    rows = _items(config)
    width = max(len(label) for label, _ in rows) + 2
    for label, state in rows:
        print(f"{label:<{width}}{state}")
    missing = [label for label, state in _items(config) if state == "缺失"]
    print()
    if missing:
        print(f"待填写 {len(missing)} 项：" + "、".join(missing))
        return 1
    print("全部已填写。")
    return 0


def _emit(text: str) -> None:
    """流程标记（frontend/app.js 的 FLOWS 文案与这里一一对应）：▶ 阶段开始 / ✓ 完成 / - 跳过。"""
    print(text, flush=True)


def _kb_build(webui: str, out: str) -> int:
    """从 webui 源码构建知识库（导航图 + 页面知识卡）；node 必须在 PATH 里。"""
    from jev_convert.kb.build import KbError, build_kb

    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    source = Path(webui) if webui else Path(config.webui.source_dir).expanduser()
    destination = _project_path(out) if out else _project_path(config.paths.data_dir) / "kb"
    stages = {"extract": "▶ 抽取路由与组件", "write": "▶ 合并生成知识卡"}
    try:
        meta = build_kb(source, destination, modules_file=config.webui.modules_file,
                        modules_export=config.webui.modules_export,
                        progress=lambda name: _emit(stages[name]))
    except KbError as error:
        print(f"知识库构建失败：{error}", file=sys.stderr)
        return 1
    _emit("✓ 合并生成知识卡")
    counts = meta["counts"]
    print(f"kb_version={meta['kb_version']} 节点 {counts['nodes']}，有菜单 {counts['menus']}，"
          f"卡片 {counts['cards']}，边 {counts['edges']}，dynamic {counts['dynamic']}")
    print(f"知识库已写出：{destination}")
    return 0


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _compile(url: str, until: str) -> int:
    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    from jev_convert.compile import library
    from jev_convert.compile.llm import Llm, write_stats
    from jev_convert.compile.pipeline import LLM_CACHE_DIR

    data_dir = _project_path(config.paths.data_dir)
    model = Llm(config, data_dir / LLM_CACHE_DIR)
    stages = {"s0": "▶ S0 读取导图", "s1": "▶ S1 识别用例", "s2": "▶ S2-S6 逐用例编译"}
    try:
        result = compile_mindnote(url, config, until=until, llm=model,
                                  progress=lambda name: _emit(stages[name]))
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    except AuthRequired as error:
        print(f"需要飞书授权：{error}\n授权链接：{error.url}", file=sys.stderr)
        return 1
    except LarkError as error:
        print(f"编译失败：{error}", file=sys.stderr)
        return 1
    except KbLoadError as error:
        print(f"编译失败，知识库读不了：{error}", file=sys.stderr)
        return 2

    _print_result(result)
    if result.plans:
        _emit("▶ 计划库写盘")
        try:
            kb_version = load_graph(data_dir / "kb").kb_version
        except KbLoadError:
            kb_version = None
        for mindnote, merged in library.save(data_dir, result.plans, kb_version=kb_version).items():
            folder = library.plans_dir(data_dir, mindnote)
            print(f"计划库 {folder}：新增 {len(merged.added)}，内容变化 {len(merged.changed)}，"
                  f"未变 {len(merged.kept)}，标为 stale {len(merged.stale)}")
        _emit("✓ 计划库写盘")
    else:
        _emit("- 计划库写盘（无计划，跳过）")
    write_stats(_project_path(config.paths.runs_dir) / "llm-stats.json", model.hits, model.misses)
    if until == "s6":
        state = _export_confirm_items(config, data_dir, result)
        if state == "ok":
            _emit("✓ 确认表导出")
        elif state == "skip":
            _emit("- 确认表导出（跳过）")
        else:
            _emit("✗ 确认表导出（失败，见上方错误）")
    return 0


def _export_confirm_items(config: Config, data_dir: Path, result: CompileResult) -> str:
    """compile 结束时把需要人工确认的内容写进确认表（Step 13）：draft 计划的待确认步骤，
    以及画像吻合度低时的写法标注样例。表未配置时跳过；导出失败不改变编译结果（可重跑，按键去重）。

    返回 "ok" / "skip" / "fail"，供流程标记用。
    """
    from jev_convert.compile.s0_read import auth_required
    from jev_convert.compile.s0_read import load as load_mindnote
    from jev_convert.confirm import base as confirm
    from jev_convert.kb.load import load_cards, load_graph

    table = confirm.table_of(config.lark)
    if table is None:
        return "skip"
    items = []
    if result.plans:
        try:
            graph = load_graph(data_dir / "kb")
            cards = load_cards(data_dir / "kb")
        except KbLoadError as error:
            print(f"确认表导出跳过：读知识库失败（{error}）", file=sys.stderr)
            return "skip"
        items = list(confirm.items_for(result.plans, graph, cards, config.thresholds.auto_confirm))
    if result.fit < config.thresholds.profile_fit:
        try:
            mindnote = load_mindnote(result.mindnote, data_dir)
        except (KeyError, TypeError, ValueError, OSError):
            mindnote = None    # source.json 读不了（比如只跑到 s0）：只导出步骤项
        if mindnote is not None:
            items += list(confirm.label_items(result.mindnote, mindnote.nodes, result.fit,
                                              config.thresholds.profile_fit))
    if not items:
        return "skip"
    client = make_lark(config)
    try:
        written = confirm.export(client, table, items)
    except MissingScope as error:
        required = auth_required(client, error)
        print(f"确认表导出需要飞书授权：{error}\n授权链接：{required.url}", file=sys.stderr)
        return "fail"
    except LarkError as error:
        print(f"确认表导出失败：{error}", file=sys.stderr)
        return "fail"
    print(f"确认表：新写入 {written} 项（draft 待确认与写法标注），处理结果用 jev-convert confirm 读回")
    return "ok"


def make_lark(config: Config):
    """真实飞书访问层（lark-cli 子进程）；测试里替换成假对象。只在 compile 导出和 confirm 里调用。"""
    from jev_convert.lark import Lark

    return Lark(config.lark.cli)


def _confirm(url: str, *, lark=None) -> int:
    """读回确认表（Step 13）：更新计划、沉淀别名知识、回写处理结果。lark 可注入（测试用）。"""
    from jev_convert.compile import library
    from jev_convert.compile.s0_read import auth_required, resolve_token
    from jev_convert.confirm import base as confirm
    from jev_convert.kb.load import load_cards, load_graph

    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    table = confirm.table_of(config.lark)
    if table is None:
        print("未配置确认表：在 qa.toml 的 [lark] 里填写 confirm_base_token 与 confirm_table_id",
              file=sys.stderr)
        return 2
    client = lark if lark is not None else make_lark(config)
    data_dir = _project_path(config.paths.data_dir)

    def _auth(error: MissingScope) -> int:
        required = auth_required(client, error)
        print(f"需要飞书授权：{error}\n授权链接：{required.url}", file=sys.stderr)
        return 1

    try:
        _emit("▶ 解析导图")
        token = resolve_token(url, client)
        _emit("✓ 解析导图")
    except AuthRequired as error:
        print(f"需要飞书授权：{error}\n授权链接：{error.url}", file=sys.stderr)
        return 1
    except MissingScope as error:
        return _auth(error)
    except LarkError as error:
        print(f"解析导图链接失败：{error}", file=sys.stderr)
        return 1
    folder = library.plans_dir(data_dir, token)
    if not library.load(folder):
        print(f"计划库 {folder} 为空：先运行 jev-convert compile <链接>", file=sys.stderr)
        return 2
    try:
        graph = load_graph(data_dir / "kb")
        cards = load_cards(data_dir / "kb")
    except KbLoadError as error:
        print(f"读取失败：{error}", file=sys.stderr)
        return 2
    stages = {"read": "▶ 读回确认表", "apply": "▶ 更新计划", "settle": "▶ 沉淀与回写"}
    try:
        collected = confirm.collect(client, table, data_dir, token, graph, cards,
                                    config.thresholds.auto_confirm,
                                    progress=lambda name: _emit(stages[name]))
    except MissingScope as error:
        return _auth(error)
    except LarkError as error:
        print(f"确认表读取失败：{error}", file=sys.stderr)
        return 1
    except KbLoadError as error:
        print(f"别名沉淀失败：{error}", file=sys.stderr)
        return 2
    _emit("✓ 沉淀与回写")

    applied = collected.applied
    for case_id in sorted(applied.plans):
        print(f"{case_id}：{applied.plans[case_id].status}")
    if collected.labels:
        print(f"写法标注：{len(collected.labels)} 个节点，下次编译生效")
    print(f"别名沉淀 {len(applied.aliases)} 条；确认 {len(applied.confirmed)}，否决 {len(applied.rejected)}，"
          f"回写确认表 {collected.rows_updated} 行")
    return 0


def _llm_stats() -> int:
    from jev_convert.compile.llm import cache_entries
    from jev_convert.compile.pipeline import LLM_CACHE_DIR

    load_environment()
    try:
        config = load_config()
    except ConfigError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    print(f"缓存条目：{cache_entries(_project_path(config.paths.data_dir) / LLM_CACHE_DIR)}")
    path = _project_path(config.paths.runs_dir) / "llm-stats.json"
    if not path.exists():
        print("最近一次编译：没有记录")
        return 0
    last = jsonio.load(path).get("last_compile", {})
    rate = last.get("hit_rate")
    shown = "—" if rate is None else f"{rate:.0%}"
    print(f"最近一次编译：命中 {last.get('hits', 0)}，请求 {last.get('misses', 0)}，命中率 {shown}")
    return 0


def _print_result(result: CompileResult) -> None:
    rows = (
        ("用例数", len(result.plans)),
        ("confirmed", result.confirmed),
        ("draft", result.draft),
        ("待确认数", result.pending),
        ("不可自动化数", result.not_automatable),
        ("写法画像吻合度", f"{result.fit:.2f}"),
    )
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{width}}：{value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-convert", description="jev-convert 命令行：测试用例转化")
    sub = parser.add_subparsers(dest="command", required=True)
    config_parser = sub.add_parser("config", help="配置相关命令")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="加载配置，逐项打印已配置 / 缺失")
    kb_parser = sub.add_parser("kb", help="知识库相关命令")
    kb_sub = kb_parser.add_subparsers(dest="kb_command", required=True)
    kb_build = kb_sub.add_parser("build", help="从 webui 源码构建导航图与页面知识卡（需要 node）")
    kb_build.add_argument("--webui", default="", help="webui 源码目录（默认取 [webui].source_dir）")
    kb_build.add_argument("--out", default="", help="输出目录（默认 <data_dir>/kb）")
    compile_parser = sub.add_parser("compile", help="把思维导图用例编译成执行计划")
    compile_parser.add_argument("url", help="飞书思维导图（mindnote）链接")
    compile_parser.add_argument(
        "--until", default="s6", choices=COMPILE_STAGES, help="跑到哪个阶段就停（默认 s6）"
    )
    confirm_parser = sub.add_parser("confirm", help="读回确认表：更新计划、沉淀知识并回写处理结果")
    confirm_parser.add_argument("url", help="飞书思维导图（mindnote）链接")
    llm_parser = sub.add_parser("llm", help="编译期 LLM 相关命令")
    llm_sub = llm_parser.add_subparsers(dest="llm_command", required=True)
    llm_sub.add_parser("stats", help="打印缓存条目数和最近一次编译的命中率")
    args = parser.parse_args(argv)

    if args.command == "config" and args.config_command == "check":
        return _config_check()
    if args.command == "kb" and args.kb_command == "build":
        return _kb_build(args.webui, args.out)
    if args.command == "compile":
        return _compile(args.url, args.until)
    if args.command == "confirm":
        return _confirm(args.url)
    if args.command == "llm" and args.llm_command == "stats":
        return _llm_stats()
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
