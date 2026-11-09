import React from "react";

export default function OverviewCards({ overview }) {
  const data = overview || {};
  const totals = (data.plans || []).reduce(
    (acc, item) => {
      acc.mindnotes += 1;
      acc.total += item.total;
      acc.confirmed += item.counts.confirmed || 0;
      acc.draft += item.counts.draft || 0;
      acc.stale += item.counts.stale || 0;
      return acc;
    }, { mindnotes: 0, total: 0, confirmed: 0, draft: 0, stale: 0 });
  const llm = data.llm || {};
  const cards = [
    { label: "知识库版本", value: data.error ? "配置缺失" : (data.kb_version || "未构建") },
    { label: "计划库", value: totals.total
        ? `${totals.total} 条 / ${totals.mindnotes} 份导图（confirmed ${totals.confirmed} · draft ${totals.draft} · stale ${totals.stale}）`
        : "空" },
    { label: "LLM 缓存", value: `${llm.cache_entries ?? 0} 条` },
    { label: "最近编译命中率", value: llm.hit_rate == null ? "—" : `${Math.round(llm.hit_rate * 100)}%` },
  ];
  return (
    <section className="cards">
      {cards.map((card) => (
        <div className="card" key={card.label}>
          <div className="label">{card.label}</div>
          <div className="value">{card.value}</div>
        </div>
      ))}
    </section>
  );
}
