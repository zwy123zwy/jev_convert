import React from "react";

// 前置依赖清单（/api/deps）：ready=true 就绪；false 且 optional 为「未配置（可选）」；false 为缺失。
export default function DepsTable({ items }) {
  const rows = (items || []).map((item) => {
    const badge = item.ready === true
      ? <span className="badge confirmed">就绪</span>
      : item.ready === false
        ? (item.optional
          ? <span className="badge draft">未配置（可选）</span>
          : <span className="badge rejected">缺失</span>)
        : <span className="badge stale">待定</span>;
    return (
      <tr key={item.id + item.label}>
        <td>{item.label}</td>
        <td>{badge}</td>
        <td className="hint">{item.hint || ""}</td>
      </tr>
    );
  });
  return (
    <table className="grid deps">
      <thead>
        <tr><th>依赖项</th><th>状态</th><th>说明</th></tr>
      </thead>
      <tbody>{rows.length ? rows : <tr><td colSpan="3" className="hint">没有检查项</td></tr>}</tbody>
    </table>
  );
}
