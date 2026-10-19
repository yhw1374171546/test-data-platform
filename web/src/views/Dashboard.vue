<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { api, urlParam } from "../api";

const day = ref(new Date().toISOString().slice(0, 10));
const loading = ref(false);
const report = ref({ items: [], totals: {} });
const defects = ref([]);
const projectId = urlParam("project_id") ? Number(urlParam("project_id")) : undefined;

const cards = computed(() => {
  const t = report.value.totals || {};
  return [
    { label: "执行总数", value: t.total ?? 0, type: "" },
    { label: "通过", value: t.passed ?? 0, type: "success" },
    { label: "失败", value: t.failed ?? 0, type: "danger" },
    { label: "阻塞 / 跳过", value: (t.blocked ?? 0) + (t.skipped ?? 0), type: "warning" },
  ];
});

async function load() {
  loading.value = true;
  try {
    const [rep, bad] = await Promise.all([
      api.daily(day.value, projectId),
      api.defects({ status: "open", limit: 10, project_id: projectId }),
    ]);
    report.value = rep;
    defects.value = bad.items || [];
  } catch (err) {
    ElMessage.error(err.friendly || "加载失败");
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div>
    <div class="page-head">
      <span class="page-title">日报看板</span>
      <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" style="width: 160px" @change="load" />
      <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
      <span class="tip">数据来自 worker 重算出的日报汇总；未解决的问题单在下方</span>
    </div>

    <div class="cards">
      <div v-for="c in cards" :key="c.label" class="card">
        <div class="label">{{ c.label }}</div>
        <div class="value" :class="c.type">{{ c.value }}</div>
      </div>
    </div>

    <h4>按项目 / 版本</h4>
    <el-table :data="report.items" border style="width: 100%" empty-text="这一天还没有汇总数据">
      <el-table-column prop="project_code" label="项目" min-width="140" />
      <el-table-column prop="version_name" label="版本" width="140" />
      <el-table-column prop="total" label="总数" width="80" />
      <el-table-column prop="passed" label="通过" width="80" />
      <el-table-column prop="failed" label="失败" width="80" />
      <el-table-column prop="blocked" label="阻塞" width="80" />
    </el-table>

    <h4>未解决问题单（最近 10 条）</h4>
    <el-table :data="defects" border style="width: 100%" empty-text="没有未解决的问题单">
      <el-table-column prop="defect_no" label="单号" width="170" />
      <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
      <el-table-column prop="severity" label="严重度" width="90" />
      <el-table-column prop="status" label="状态" width="100" />
      '<el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ (row.created_at || "").slice(0, 19).replace("T", " ") }}</template>
      </el-table-column>'
    </el-table>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
.card { background: #f7f9fc; border: 1px solid #ebeef5; border-radius: 8px; padding: 14px 16px; }
.label { font-size: 12px; color: #909399; }
.value { font-size: 26px; font-weight: 600; color: #1f3a5f; margin-top: 6px; }
.value.success { color: #2e9e5b; }
.value.danger { color: #c45656; }
.value.warning { color: #c08a2e; }
h4 { margin: 18px 0 8px; font-size: 13px; color: #606266; font-weight: 600; }
</style>
