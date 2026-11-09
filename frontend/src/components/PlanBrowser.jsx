import React, { useEffect, useState } from "react";
import { api } from "../api.js";

const esc = (value) => String(value ?? "").replace(/[&<>"]/g,
  (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));

// 计划库浏览：overview.plans 提供导图列表，选中后拉用例表，点行看步骤详情。
export default function PlanBrowser({ overview, onChanged }) {
  const plans = (overview && overview.plans) || [];
  const [token, setToken] = useState("");
  const [cases, setCases] = useState({});
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);

  // 导图列表变化（首次加载 / 新编译完成）时保持选中项，否则取第一个。
  useEffect(() => {
    if (!plans.length) { setToken(""); setCases({}); return; }
    const keep = plans.find((item) => item.token === token);
    const next = keep ? keep.token : plans[0].token;
    if (next !== token) setToken(next);
  }, [plans, token]);

  useEffect(() => {
    if (!token) { setCases({}); return; }
    api(`/api/plans?token=${encodeURIComponent(token)}`).then(({ data }) => {
      setCases(data && !data.error ? data : {});
    });
  }, [token, onChanged]);

  const showDetail = async (caseId) => {
    setSelected(caseId);
    const { data } = await api(`/api/plan?token=${encodeURIComponent(token)}&case=${encodeURIComponent(caseId)}`);
    setDetail(data.error ? null : data);
  };

  const entries = Object.entries(cases).sort(([a], [b]) => a.localeCompare(b));
  const steps = detail ? [...(detail.steps || []),
    ...(detail.teardown || []).map((step) => ({ ...step, teardown: true }))] : [];

  return (
    <>
      <div className="row">
        <select value={token} onChange={(e) => { setToken(e.target.value); setSelected(null); setDetail(null); }}>
          {plans.length
            ? plans.map((item) => <option key={item.token} value={item.token}>{item.token}（{item.total} 条）</option>)
            : <option value="">（计划库为空）</option>}
        </select>
        <span className="hint">{entries.length} 条</span>
      </div>
      <table className="grid">
        <thead><tr><th>用例 ID</th><th>状态</th><th>导图节点</th><th>知识库</th></tr></thead>
        <tbody>
          {entries.length ? entries.map(([caseId, item]) => (
            <tr key={caseId} className={selected === caseId ? "selected" : ""} onClick={() => showDetail(caseId)}>
              <td>{caseId}</td>
              <td><span className={`badge ${esc(item.status)}`}>{esc(item.status)}</span></td>
              <td>{esc(item.node_id || "")}</td>
              <td>{esc(item.kb_version || "")}</td>
            </tr>
          )) : <tr><td colSpan="4" className="hint">编译一份导图后这里会出现计划</td></tr>}
        </tbody>
      </table>

      {detail && (
        <div className="detail">
          <h3>{detail.id} {detail.title}</h3>
          <div className="hint">
            状态 {detail.status} · 入口 {detail.entry ? detail.entry.node : "—"}
            （{detail.entry ? detail.entry.route : "—"}）· kb {detail.kb_version || "—"}
            {detail.destructive ? " · 会改数据" : ""}
          </div>
          <table className="grid">
            <thead><tr><th>#</th><th>类型</th><th>页面</th><th>目标</th><th>置信度</th><th>断言</th></tr></thead>
            <tbody>
              {steps.map((step) => (
                <tr key={`${step.seq}-${step.teardown ? "t" : "s"}`}>
                  <td>{step.seq}</td>
                  <td>{step.kind}{step.teardown ? "（teardown）" : ""}</td>
                  <td>{step.page || ""}</td>
                  <td>{step.target && step.target.name ? step.target.name : "（待确认）"}</td>
                  <td>{step.target ? Number(step.target.confidence || 0).toFixed(2) : ""}</td>
                  <td>{(step.expect || []).join("；")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
