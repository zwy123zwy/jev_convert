import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "../api.js";

// 流程阶段文案与 jev_convert/cli.py 的 _emit 标记一一对应（▶ 开始 / ✓ 完成 / - 跳过 / ✗ 失败）。
const FLOWS = {
  compile: ["S0 读取导图", "S1 识别用例", "S2-S6 逐用例编译", "计划库写盘", "确认表导出"],
  confirm: ["解析导图", "读回确认表", "更新计划", "沉淀与回写"],
  "kb-build": ["抽取路由与组件", "合并生成知识卡"],
};

function FlowStrip({ kind, log, status }) {
  const stages = FLOWS[kind];
  if (!stages) return <div className="flow hidden" />;
  const started = {}, finished = {}, skipped = {}, failed = new Set();
  for (const line of (log || "").split("\n")) {
    const mark = line.match(/^([▶✓\-✗])\s(.+)$/);
    if (!mark) continue;
    const name = mark[2].split("（")[0].trim();
    const chip = stages.find((stage) => name === stage || name.startsWith(stage));
    if (!chip) continue;
    if (mark[1] === "▶") started[chip] = true;
    else if (mark[1] === "✓") finished[chip] = true;
    else if (mark[1] === "-") skipped[chip] = true;
    else failed.add(chip);
  }
  const startedStages = stages.filter((name) => started[name]);
  const lastStarted = startedStages.length ? startedStages[startedStages.length - 1] : null;
  const state = (name) => {
    if (failed.has(name)) return "failed";
    if (finished[name]) return "done";
    if (skipped[name] && !started[name]) return "skip";
    if (started[name]) {
      if (name !== lastStarted) return "done";          // 后续阶段已开始：隐式完成
      if (status === "running") return "active";
      return status === "done" ? "done" : "failed";
    }
    return "pending";
  };
  return (
    <div className="flow">
      {stages.map((name, index) => (
        <React.Fragment key={name}>
          {index > 0 && <span className="arrow">→</span>}
          <span className={`chip ${state(name)}`}>{name}</span>
        </React.Fragment>
      ))}
    </div>
  );
}

export function Left({ onSubmitted }) {
  const [compileUrl, setCompileUrl] = useState("");
  const [until, setUntil] = useState("s6");
  const [confirmUrl, setConfirmUrl] = useState("");
  const [kbWebui, setKbWebui] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (path, body) => {
    setBusy(true);
    const { ok, data } = await post(path, body);
    setBusy(false);
    if (!ok || !data.task) { alert(data.error || "提交失败"); return; }
    onSubmitted(data.task);
  };

  return (
    <>
      <form className="op" onSubmit={(e) => { e.preventDefault(); submit("/api/compile", { url: compileUrl.trim(), until }); }}>
        <h3>1 · 编译导图</h3>
        <input type="text" placeholder="飞书思维导图链接" required value={compileUrl}
               onChange={(e) => setCompileUrl(e.target.value)} />
        <label className="inline">编译到
          <select value={until} onChange={(e) => setUntil(e.target.value)}>
            <option value="s0">s0 读取</option>
            <option value="s1">s1 识别</option>
            <option value="s2">s2 规范化</option>
            <option value="s3">s3 落地</option>
            <option value="s4">s4 路线</option>
            <option value="s5">s5 断言</option>
            <option value="s6">s6 校验（完整）</option>
          </select>
        </label>
        <button type="submit" disabled={busy}>开始编译</button>
      </form>

      <form className="op" onSubmit={(e) => { e.preventDefault(); submit("/api/confirm", { url: confirmUrl.trim() }); }}>
        <h3>2 · 读回确认表</h3>
        <input type="text" placeholder="同一个导图链接" required value={confirmUrl}
               onChange={(e) => setConfirmUrl(e.target.value)} />
        <p className="hint">人在多维表格里填完结论后，点这里更新计划、沉淀别名并回写。</p>
        <button type="submit" disabled={busy}>读回确认表</button>
      </form>

      <form className="op" onSubmit={(e) => { e.preventDefault(); submit("/api/kb-build", { webui: kbWebui.trim() }); }}>
        <h3>3 · 构建知识库</h3>
        <input type="text" placeholder="webui 源码目录（留空用 qa.toml 的 [webui].source_dir）"
               value={kbWebui} onChange={(e) => setKbWebui(e.target.value)} />
        <p className="hint">需要 node 在 PATH 中；首次使用先在 jev_convert/kb/extract 下 npm install。</p>
        <button type="submit" disabled={busy}>构建 / 重建知识库</button>
      </form>
    </>
  );
}

export function Right({ taskId, onChanged }) {
  const [tasks, setTasks] = useState([]);
  const [current, setCurrent] = useState(null);
  const [log, setLog] = useState("（还没有任务）");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const timer = useRef(null);
  const logRef = useRef(null);
  const currentRef = useRef(null);

  const stop = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null; } };
  const select = (id) => { currentRef.current = id; setCurrent(id); };

  const loadTasks = useCallback(async () => {
    const { data } = await api("/api/tasks");
    const list = Array.isArray(data) ? data : [];
    setTasks(list);
    return list;
  }, []);

  const poll = useCallback(async (id) => {
    const { data } = await api(`/api/task/${id}`);
    if (data.error) { setLog(data.error); return; }
    setLog(data.log || "（运行中，尚无输出）");
    setStatus(data.status);
    setKind(data.kind);
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
    if (data.status === "running") {
      stop();
      timer.current = setTimeout(() => poll(id), 1000);
      loadTasks();      // running 期间也刷新任务列表：新任务的 tab 立即出现
    } else {
      await loadTasks();   // 任务到终态：刷新任务列表的状态徽标
      onChanged();
    }
  }, [onChanged, loadTasks]);

  // 挂载一次：自动选中最新任务并开始轮询
  useEffect(() => {
    let cancelled = false;
    loadTasks().then((list) => {
      if (cancelled || currentRef.current || !list.length) return;
      select(list[0].id);
      poll(list[0].id);
    });
    return () => { cancelled = true; stop(); };
  }, [loadTasks, poll]);

  // 新提交的任务（App 传入）：立即选中并轮询
  useEffect(() => {
    if (!taskId) return;
    select(taskId);
    stop();
    poll(taskId);
  }, [taskId, poll]);

  return (
    <>
      <FlowStrip kind={kind} log={log} status={status} />
      <div className="task-tabs">
        {tasks.map((task) => (
          <span key={task.id}
                className={`task-tab ${task.id === current ? "active" : ""} ${task.status === "running" ? "running" : ""}`}
                onClick={() => { if (currentRef.current !== task.id) { select(task.id); poll(task.id); } }}>
            {task.id} · {task.kind} <span className={`badge ${task.status}`}>{task.status}</span>
          </span>
        ))}
      </div>
      <pre ref={logRef} className="log">{log}</pre>
    </>
  );
}

export default { Left, Right };
