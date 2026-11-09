<template>
  <div class="task-pool-filter-page">
    <h3 class="filter-card__title">基本信息</h3>
    <el-button type="primary" @click="start">开始处理</el-button>
    <el-button v-if="canBatch" :loading="loading">批量退回</el-button>
    <el-button type="text">{{ buttonText }}</el-button>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="待处理" name="pending" />
      <el-tab-pane :label="dynamicTabLabel" name="dynamic" />
    </el-tabs>

    <el-form :model="filterForm" label-position="top">
      <el-form-item label="处理任务ID">
        <el-input v-model="filterForm.dataJobId" placeholder="请输入处理任务ID" />
      </el-form-item>
      <el-form-item :label="collectorLabel">
        <el-select v-model="filterForm.collectorId" />
      </el-form-item>
    </el-form>

    <el-table :data="rows">
      <el-table-column label="任务ID" prop="dataJobId" />
      <el-table-column :label="columnLabel" prop="status" />
    </el-table>

    <el-dialog v-model="dialogVisible" title="确认退回">
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirm">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute } from "vue-router";

const route = useRoute();
const activeTab = ref("pending");
const dialogVisible = ref(false);
const loading = ref(false);
const canBatch = ref(false);
const buttonText = ref("查看详情");
const dynamicTabLabel = ref("动态");
const collectorLabel = ref("采集员");
const columnLabel = ref("状态");

const getQuery = (key: string) => {
  const value = route.query[key];
  return typeof value === "string" ? value : "";
};

const graphDefinitionId = route.query.graph_definition_id as string;
const nodeId = route.query["node_id"] as string;
const flow = getQuery("flow");
// 下标是变量，不是字面量：抽取器不应把它记成 required_query
const dynamicKey = "node";
const dynamicValue = route.query[dynamicKey];
console.log(nodeId, flow, dynamicValue);

function start() {
  // ElMessage.warning("这句在注释里，不应被抽到");
  if (!graphDefinitionId) {
    ElMessage.warning("请先选择流程");
    return;
  }
  ElMessage.success("开始处理成功");
}

function confirm() {
  ElMessage({ message: "对象写法不是字面量，不抽取" });
}
</script>
