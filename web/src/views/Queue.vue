<script setup>
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, urlParam } from "../api";

const loading = ref(false);
const options = reactive({ testers: [], projects: [] });
const testerId = ref(null);
// 支持 ?project_id=4 直接筛某个项目（演示/分享链接用）
const projectId = ref(urlParam("project_id") ? Number(urlParam("project_id")) : null);
const status = ref("pending");
const tasks = ref([]);

async function loadOptions() {
  try {
    const data = await api.options();
    options.testers = data.testers || [];
    options.projects = data.projects || [];
    testerId.value = options.testers[0]?.id ?? null;
  } catch (err) {
    ElMessage.error(err.friendly || "字典加载失败");
  }
}

async function load() {
  loading.value = true;
  try {
    const data = await api.queue({ status: status.value, limit: 50, project_id: projectId.value || undefined });
    tasks.value = data.items || [];
  } catch (err) {
    ElMessage.error(err.friendly || "加载失败");
  } finally {
    loading.value = false;
  }
}

async function claim(row) {
  if (!testerId.value) {
    ElMessage.warning("请选择测试员");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `将任务 #${row.task_id}（${row.case_no}）领取给测试员，确认？`,
      "领取任务",
      { type: "info", confirmButtonText: "领取", cancelButtonText: "取消" }
    );
  } catch {
    return;
  }
  try {
    await api.claim({ task_id: row.task_id, tester_id: testerId.value });
    ElMessage.success(`已领取 #${row.task_id}`);
    await load();
  } catch (err) {
    // 409 already_claimed 说明被别人抢先了 —— 这正是要演示的并发语义
    ElMessage.warning(err.friendly || "领取失败");
  }
}

onMounted(async () => {
  await loadOptions();
  await load();
});
</script>

<template>
  <div>
    <div class="page-head">
      <span class="page-title">任务领取</span>
      <el-select v-model="projectId" placeholder="全部项目" clearable style="width: 220px" @change="load">
        <el-option v-for="p in options.projects" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
      </el-select>
      <el-select v-model="status" style="width: 140px" @change="load">
        <el-option label="待领取" value="pending" />
        <el-option label="已领取" value="claimed" />
        <el-option label="已完成" value="done" />
        <el-option label="全部" value="" />
      </el-select>
      <el-select v-model="testerId" placeholder="领取给" style="width: 200px">
        <el-option v-for="t in options.testers" :key="t.id" :label="`${t.employee_no} · ${t.name}`" :value="t.id" />
      </el-select>
      <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      <span class="tip">同一任务只有一个领取人；重复领取会返回 409</span>
    </div>

    <el-table :data="tasks" border style="width: 100%" empty-text="没有任务">
      <el-table-column prop="task_id" label="任务号" width="90" />
      <el-table-column prop="case_no" label="用例编号" width="150" />
      <el-table-column prop="case_title" label="用例标题" min-width="220" show-overflow-tooltip />
      <el-table-column prop="priority" label="优先级" width="90" />
      <el-table-column prop="ver" label="版本" width="120" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'pending' ? 'warning' : 'success'" effect="plain">
            {{ row.status }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-button size="small" type="primary" :disabled="row.status !== 'pending'" @click="claim(row)">
            领取
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>
