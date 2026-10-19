<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";

const loading = ref(false);
const options = reactive({ projects: [], versions: [], vehicles: [], testers: [] });
const vin = ref("");
const day = ref(new Date(Date.now() + 86400000).toISOString().slice(0, 10));
const slots = ref([]);

const dialog = reactive({ visible: false, half: 0, projectId: null, testerId: null, busy: false });

// 始终展示「上午 / 下午」两行：那天还没有档期行时也要能直接预约
// （后端下单时会按需物化档期行，前端不需要先建）
const rows = computed(() =>
  [0, 1].map((half) => {
    const found = slots.value.find((s) => s.half === half);
    const status = found?.status || "free";
    return {
      half,
      label: half === 0 ? "上午" : "下午",
      slot_date: found?.slot_date || day.value,
      status,
      free: status === "free",
    };
  })
);

const projectsForVin = computed(() => {
  const vehicle = options.vehicles.find((v) => v.vin === vin.value);
  if (!vehicle) return options.projects;
  const matched = options.projects.filter((p) => p.model === vehicle.model);
  return matched.length ? matched : options.projects;
});

async function loadOptions() {
  try {
    Object.assign(options, await api.options());
    vin.value = options.vehicles[0]?.vin || "";
    dialog.projectId = projectsForVin.value[0]?.id ?? null;
    dialog.testerId = options.testers[0]?.id ?? null;
  } catch (err) {
    ElMessage.error(err.friendly || "字典加载失败");
  }
}

async function loadSlots() {
  if (!vin.value) return;
  loading.value = true;
  try {
    const data = await api.slots(vin.value, day.value);
    slots.value = data.slots || [];
  } catch (err) {
    ElMessage.error(err.friendly || "查询失败");
  } finally {
    loading.value = false;
  }
}

function openBook(row) {
  dialog.half = row.half;
  dialog.projectId = projectsForVin.value[0]?.id ?? null;
  dialog.visible = true;
}

async function confirmBook() {
  if (!dialog.projectId || !dialog.testerId) {
    ElMessage.warning("请选择项目与测试员");
    return;
  }
  dialog.busy = true;
  try {
    await api.book({
      project_id: dialog.projectId,
      vehicle_id: options.vehicles.find((v) => v.vin === vin.value)?.id,
      testers_id: dialog.testerId,
      slot_date: day.value,
      half: dialog.half,
    });
    ElMessage.success("预约成功");
    dialog.visible = false;
    await loadSlots();
  } catch (err) {
    // 409 slot_not_available 是并发下的正常结果，提示换档期即可
    ElMessage.error(err.friendly || "预约失败");
  } finally {
    dialog.busy = false;
  }
}

onMounted(async () => {
  await loadOptions();
  await loadSlots();
});
</script>

<template>
  <div>
    <div class="page-head">
      <span class="page-title">档期预约</span>
      <el-select v-model="vin" placeholder="选择车辆" style="width: 260px" @change="loadSlots">
        <el-option v-for="v in options.vehicles" :key="v.vin" :label="`${v.model} · ${v.vin}`" :value="v.vin" />
      </el-select>
      <el-date-picker v-model="day" type="date" value-format="YYYY-MM-DD" style="width: 160px" @change="loadSlots" />
      <el-button type="primary" :loading="loading" @click="loadSlots">查询</el-button>
      <span class="tip">一天分上午 / 下午两个档期；同一档期只能被一个项目占用</span>
    </div>

    <el-table :data="rows" border style="width: 100%" empty-text="当天没有档期">
      <el-table-column label="档期" width="100">
        <template #default="{ row }">{{ row.label }}</template>
      </el-table-column>
      <el-table-column prop="slot_date" label="日期" width="130" />
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.free ? 'success' : 'info'" effect="plain">{{ row.free ? "可预约" : "已占用" }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="120">
        <template #default="{ row }">
          <el-button size="small" type="primary" :disabled="!row.free" @click="openBook(row)">预约</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog.visible" title="预约档期" width="420px">
      <el-form label-width="80px">
        <el-form-item label="车辆">{{ vin }}</el-form-item>
        <el-form-item label="日期">{{ day }}</el-form-item>
        <el-form-item label="档期">{{ dialog.half === 0 ? "上午" : "下午" }}</el-form-item>
        <el-form-item label="项目">
          <el-select v-model="dialog.projectId" style="width: 100%">
            <el-option v-for="p in projectsForVin" :key="p.id" :label="`${p.code} · ${p.name}`" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="测试员">
          <el-select v-model="dialog.testerId" style="width: 100%">
            <el-option v-for="t in options.testers" :key="t.id" :label="`${t.employee_no} · ${t.name}`" :value="t.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="dialog.busy" @click="confirmBook">确认预约</el-button>
      </template>
    </el-dialog>
  </div>
</template>
