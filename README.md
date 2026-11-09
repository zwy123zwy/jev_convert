# jev-convert · 测试用例转化

把飞书测试用例思维导图编译成**可执行、可人工确认**的测试计划。是 [jev-qa](https://github.com/zwy123zwy/jev-qa)（浏览器测试 Agent）的「用例转化」独立版：本仓库只做转化，不做执行。

```
飞书思维导图 ──▶ S0 读取 ─▶ S1 写法画像识别 ─▶ S2 步骤规范化 ─▶ S3 页面/元素落地
            ─▶ S4 路线 ─▶ S5 断言 ─▶ S6 静态校验 ─▶ 计划库（qa_data/plans/，入 git）
                                            │
                    draft 待确认项 ──▶ 飞书多维表格（人工确认）──▶ jev-convert confirm 读回
                                            │                        │
                                            └── confirmed 计划 ◀── 别名沉淀进知识库
```

产出的执行计划交给 jev-qa 执行：`jev-qa run --plans qa_data/plans/<导图>`。

## 功能

- **知识库**：从 webui 源码（Vue 路由 + 组件）抽取导航图与页面知识卡，S3 落地以它为依据。
- **写法画像**：每份导图推断一份「画像」识别用例结构，规则不写死；吻合度低时自动把标注样例导出到确认表，人工标完下次编译直接生效。
- **编译期 LLM 全部收敛为候选选择**：模型只在候选里选、输出经代码校验，结果按内容哈希缓存（可复现、可统计命中率）。
- **计划库**：按导图分目录增量合并——原文没变就保留人工确认的结果和学到的定位；导图节点删除或知识库重建后相关计划标 `stale`。
- **人工确认闭环**：draft 步骤 + 候选写进多维表格 → 人工填结论 → 读回更新计划并重过 S6 → 确认过的「说法 → 元素」沉淀进知识库别名表。
- **可视化前端**：本地 Web 页面提交编译、实时看阶段流程与日志、浏览计划库详情、触发确认读回。

## 目录结构

```
jev_convert/
├── cli.py              # CLI：config check / kb build / compile / confirm / llm stats
├── compile/            # S0–S6 编译流水线 + 写法画像 + 计划库（library.py）
├── confirm/            # 飞书多维表格确认（导出 items_for / 读回 collect / 沉淀 record_knowledge）
├── kb/                 # 知识库：build.py（入口）+ extract/（Node 脚本解析 Vue）
├── lark.py             # lark-cli 封装（wiki / mindnote / base 记录读写）
├── verify/dsl.py       # 断言 DSL（url / element / text / row / semantic…），S5 生成、S6 校验
├── config.py  jsonio.py
frontend/               # 可视化前端（原生 HTML/JS/CSS，无外部依赖）
server.py               # 本地服务（纯标准库 http.server，只监听 127.0.0.1）
tests/                  # pytest（不联网、不调 lark-cli、不启动浏览器）
```

## 快速开始

```bash
# 1. 安装（Python ≥ 3.12，uv）
uv sync

# 2. 知识库抽取脚本依赖 Node：首次使用先装依赖
npm install --prefix jev_convert/kb/extract

# 3. 配置
cp qa.example.toml qa.toml        # 填写 webui 源码目录、飞书确认表、LLM key 等
cp .env.example .env              # 密钥只放这里（TEXT_MODEL_API_KEY 等）

# 4. 构建知识库（读 webui 源码，写 qa_data/kb/）
uv run jev-convert kb build

# 5. 编译一份导图
uv run jev-convert compile "https://…feishu.cn/wiki/…" --until s6
```

或使用可视化前端（与 CLI 等价，编译/确认/构建都在页面上操作）：

```bash
uv run python server.py           # 打开 http://127.0.0.1:8765
```

页面功能：统计卡片（知识库版本 / 计划库计数 / LLM 命中率）、编译任务（S0–S6 流程条实时点亮）、确认表读回、知识库重建、计划库浏览（按用例查看步骤、目标与断言）。

## 配置要点（qa.toml）

| 配置 | 说明 |
| --- | --- |
| `[webui].source_dir` | 被测前端源码目录（只读），知识库从这里抽取 |
| `[compile_llm]` | 编译期 LLM（OpenAI 兼容接口），密钥走 `.env` |
| `[lark].confirm_base_token` / `confirm_table_id` | 确认表的多维表格（表需事先按 `jev_qa/confirm/base.py` 顶部字段清单建好） |
| `[thresholds].profile_fit` / `auto_confirm` | 画像吻合度阈值（低于则转人工标注）、自动确认的落地置信度 |
| `[test_data]` | 固定测试数据，计划里以 `${data.xxx}` 引用 |

计划里还有两类引用：`${account.*}`（凭据，只放在 `.env`，执行侧在动作层替换）、`${<数据名>.<列名>}`（执行侧数据池领取）。

## 常用命令

```bash
uv run jev-convert config check              # 逐项检查配置（密钥只显示已设置/未设置）
uv run jev-convert kb build [--webui <目录>] # 重建知识库；learned.json / confirmed.json 不受影响
uv run jev-convert compile <链接> [--until sN]  # 编译；s6 结束后自动导出确认表
uv run jev-convert confirm <链接>            # 读回确认表：更新计划、沉淀别名、回写处理结果
uv run jev-convert llm stats                 # LLM 缓存条目数与最近一次编译命中率
```

## 与 jev-qa 的分工

| | jev-convert（本仓库） | jev-qa |
| --- | --- | --- |
| 职责 | 导图 → 计划（含人工确认） | 计划 → 浏览器执行 → 报告 / 训练数据 |
| 关键产物 | `qa_data/plans/<导图>/<用例>.json`、`qa_data/kb/` | `qa_runs/`（journal、trace、报告）、决策缓存 |
| 技术 | httpx + lark-cli + Node 抽取脚本 | Playwright + Jev 决策模型 |

两仓库通过 `qa_data/plans/` 的计划文件衔接，可独立演进。

## 开发

```bash
uv run pytest          # 378 个测试，全部离线
uv run ruff check .    # 行宽 120，规则 E/F/I
```

约定：数据文件一律键排序、固定缩进、原子写入（同一输入两次写盘字节一致）；pytest 不访问外部系统；涉及飞书 / LLM 的调用全部通过可注入的假对象测试。
