import PageComponent from "../views/demo/Page.vue";
import { type RouteConfig } from "./types";

// 标题来自常量：抽取器应输出 dynamic，绝不猜成 "任务池筛选"
const POOL_TITLE = "任务池筛选";

export const routes: RouteConfig[] = [
  { path: "/login", redirect: "/internal-login" },
  {
    path: "/internal-login",
    name: "InternalLogin",
    component: () => import("@/views/login/internalLogin.vue"),
    meta: { title: "内部登录" },
  },
  {
    path: "/new/data-platform",
    name: "DataPlatformNew",
    component: () => import("@/views/dataPlatformNew/index.vue"),
    redirect: "/new/data-platform/task/process",
    meta: { title: "数据平台-新版", icon: "Check", backendKey: "data_platform_new" },
    children: [
      {
        path: "task",
        name: "DataPlatformNewTask",
        component: () => import("@/views/dataPlatformNew/task/index.vue"),
        redirect: "/new/data-platform/task/process",
        meta: { title: "任务管理", backendKey: "data_platform_new_task_management" },
        children: [
          {
            path: "process",
            name: "DataPlatformNewProcessTask",
            component: () => import("@/views/dataPlatformNew/task/ProcessTask.vue"),
            meta: { title: "处理任务", backendKey: "data_platform_new_process_task" },
          },
        ],
      },
      {
        path: "workbench",
        name: "DataPlatformNewWorkbench",
        component: () => import("@/views/dataPlatformNew/workbench/index.vue"),
        redirect: "/new/data-platform/workbench/annotation",
        meta: { title: "工作台", backendKey: "data_platform_new_workbench" },
        children: [
          {
            path: "annotation",
            name: "DataPlatformNewAnnotationWorkbench",
            component: () => import("@/views/dataPlatformNew/workbench/AnnotationWorkbench.vue"),
            meta: { title: "标注工作台", backendKey: "data_platform_new_annotation_workbench" },
          },
          {
            path: "annotation/task-pool-filter",
            name: "DataPlatformNewAnnotationTaskPoolFilter",
            component: () => Promise.resolve(PageComponent),
            meta: {
              title: POOL_TITLE,
              hidden: true,
              activeMenu: "/new/data-platform/workbench/annotation",
              backendKey: "data_platform_new_annotation_workbench",
            },
          },
        ],
      },
      {
        path: "batch",
        name: "DataPlatformNewBatchList",
        component: () => import("@/views/dataPlatformNew/batch/BatchList.vue"),
        meta: { title: "批次列表", backendKey: "data_platform_new_batch_list" },
      },
    ],
  },
  {
    path: "/403",
    name: "403",
    component: () => import("@/views/error/403.vue"),
    meta: { title: "权限不足" },
  },
];
