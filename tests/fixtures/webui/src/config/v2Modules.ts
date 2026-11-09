/** 精简片段的菜单配置：模块 → 容器 → 叶子 */

export interface V2Leaf {
  backendKey: string;
  title?: string;
}

export interface V2Container {
  key: string;
  title?: string;
  name?: string;
  fromBackendKey?: string;
  icon?: string;
  leaves: V2Leaf[];
}

export type V2Entry = V2Container | V2Leaf;

export interface V2Module {
  key: string;
  name: string;
  icon: string;
  entries: V2Entry[];
  homeBackendKey?: string;
}

// 模块名来自常量：该模块下的叶子不应生成菜单路径
const SAAS_MODULE_NAME = "SaaS 数据平台";

export const V2_MODULES: V2Module[] = [
  {
    key: "data-platform-new",
    name: "数据平台-新版",
    icon: "Check",
    homeBackendKey: "data_platform_new_process_task",
    entries: [
      {
        key: "c_data_platform_new_task",
        fromBackendKey: "data_platform_new_task_management",
        icon: "Management",
        leaves: [{ backendKey: "data_platform_new_process_task" }],
      },
      {
        key: "c_data_platform_new_workbench",
        title: "工作台",
        icon: "Workbench",
        leaves: [{ backendKey: "data_platform_new_annotation_workbench", title: "标注工作台" }],
      },
      { backendKey: "data_platform_new_batch_list", title: "批次列表" },
    ],
  },
  {
    key: "saas",
    name: SAAS_MODULE_NAME,
    icon: "Setting",
    entries: [{ backendKey: "data_platform_new_task_management", title: "任务管理" }],
  },
];
