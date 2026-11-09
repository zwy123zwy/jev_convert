import React from "react";

// 执行流程图：知识库 → 编译 → 计划库 → 人工确认 → jev-qa 执行。
// 节点状态由前置依赖检查（/api/deps）与计划库统计（/api/overview）推导。
export default function Pipeline({ overview, deps }) {
  const items = {};
  for (const item of (deps && deps.items) || []) items[item.id] = item;
  const ready = (id) => (items[id] ? items[id].ready === true : false);
  const plans = (overview ? overview.plans : []).reduce(
    (acc, item) => {
      acc.total += item.total;
      acc.confirmed += item.counts.confirmed || 0;
      acc.draft += item.counts.draft || 0;
      acc.stale += item.counts.stale || 0;
      return acc;
    }, { total: 0, confirmed: 0, draft: 0, stale: 0 });
  const gates = ["config", "webui", "node", "node_modules", "llm", "kb", "lark"];
  const compileReady = gates.every(ready);
  const missing = gates.filter((id) => !ready(id)).length;
  const tableReady = ready("table");
  const nodes = [
    { title: "知识库", state: ready("kb") ? "ready" : "missing",
      sub: ready("kb") ? items.kb.hint : "未构建：kb build" },
    { title: "S0–S6 编译", state: compileReady ? "ready" : "blocked",
      sub: compileReady ? "前置已就绪，可提交编译" : `前置缺失 ${missing} 项（见下方清单）` },
    { title: "计划库", state: plans.total ? "ready" : "pending",
      sub: plans.total
        ? `${plans.total} 条（confirmed ${plans.confirmed} · draft ${plans.draft} · stale ${plans.stale}）`
        : "空：还没有编译过" },
    { title: "人工确认（多维表格）", state: tableReady ? "ready" : "pending",
      sub: tableReady ? "已配置：compile 自动导出、confirm 读回" : "未配置：导出与读回将跳过" },
    { title: "jev-qa 执行", state: "external",
      sub: "confirmed 计划交给 jev-qa run 执行（另一个项目）" },
  ];
  return (
    <div className="pipeline">
      {nodes.map((node, index) => (
        <React.Fragment key={node.title}>
          {index > 0 && <span className="parrow">→</span>}
          <div className={`pnode ${node.state}`}>
            <div className="ptitle">{node.title}</div>
            <div className="psub">{node.sub}</div>
          </div>
        </React.Fragment>
      ))}
    </div>
  );
}
