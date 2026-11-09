import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import OverviewCards from "./components/OverviewCards.jsx";
import Pipeline from "./components/Pipeline.jsx";
import DepsTable from "./components/DepsTable.jsx";
import TaskPanel from "./components/TaskPanel.jsx";
import PlanBrowser from "./components/PlanBrowser.jsx";

export default function App() {
  const [overview, setOverview] = useState(null);
  const [deps, setDeps] = useState(null);
  const [taskId, setTaskId] = useState(null);

  const refresh = useCallback(async () => {
    const [ov, dp] = await Promise.all([api("/api/overview"), api("/api/deps")]);
    setOverview(ov.data);
    setDeps(dp.data);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const onSubmitted = useCallback((id) => {
    setTaskId(id);        // TaskPanel.Right 响应变化：选中该任务并开始轮询
  }, []);

  return (
    <>
      <header>
        <h1>jev-convert</h1>
        <p>测试用例转化：导图 → 编译（S0–S6）→ 计划库 → 人工确认 → 交给 jev-qa 执行</p>
      </header>

      <OverviewCards overview={overview} />

      <section className="panel wide">
        <h2>执行流程</h2>
        <Pipeline overview={overview} deps={deps} />
        <h3 className="deps-title">
          前置依赖 <span className="hint">执行之前必须就绪的内容；缺失项先处理后，下方表单才能成功</span>
        </h3>
        <DepsTable items={deps ? deps.items : []} />
      </section>

      <main>
        <section className="panel">
          <h2>操作</h2>
          <TaskPanel.Left onSubmitted={onSubmitted} />
        </section>
        <section className="panel grow">
          <h2>执行结果</h2>
          <TaskPanel.Right taskId={taskId} onChanged={refresh} />
        </section>
      </main>

      <section className="panel wide">
        <h2>计划库</h2>
        <PlanBrowser overview={overview} onChanged={taskId} />
      </section>

      <footer>只监听本机。编译产物写在 qa_data/plans/（入 git 可评审），知识库在 qa_data/kb/；执行由 jev-qa 完成。</footer>
    </>
  );
}
