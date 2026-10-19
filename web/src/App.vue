<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api, session } from "./api";
import Login from "./views/Login.vue";
import Slots from "./views/Slots.vue";
import Queue from "./views/Queue.vue";
import Dashboard from "./views/Dashboard.vue";

const token = ref(session.token());
const role = ref(session.role());
const username = ref(session.name());
const HASHES = ["slots", "queue", "dashboard"];
const initial = location.hash.replace("#", "");
const active = ref(HASHES.includes(initial) ? initial : "slots");
const ready = ref("");

const tabs = [
  { key: "slots", label: "档期预约" },
  { key: "queue", label: "任务领取" },
  { key: "dashboard", label: "日报看板" },
];

const view = computed(() => ({ slots: Slots, queue: Queue, dashboard: Dashboard }[active.value]));

// 用 hash 记住当前页：刷新不回到第一个 tab，也方便直接分享 #queue 这种链接
watch(active, (v) => {
  if (location.hash.replace("#", "") !== v) location.hash = v;
});
window.addEventListener("hashchange", () => {
  const hash = location.hash.replace("#", "");
  if (HASHES.includes(hash) && hash !== active.value) active.value = hash;
});

function onLogin(data) {
  token.value = data.access_token;
  role.value = data.role;
  username.value = data.username;
  ElMessage.success(`欢迎，${data.username}`);
}

function logout() {
  session.clear();
  token.value = "";
  role.value = "";
  username.value = "";
}

onMounted(async () => {
  // 联调入口：URL 上带 ?token=xxx 就直接用这个 token（方便截图、演示、分享链接）。
  // 不带的话就走正常登录流程。token 只存在 localStorage，不写进代码。
  const urlToken = new URLSearchParams(location.search).get("token");
  if (urlToken && !token.value) {
    session.save({ access_token: urlToken, role: "", username: "" });
    token.value = urlToken;
    try {
      const me = await api.me();
      role.value = me.role;
      username.value = me.username;
      session.save({ access_token: urlToken, role: me.role, username: me.username });
    } catch {
      /* 拿不到身份就让用户自己重新登录，不阻塞页面 */
    }
  }
  if (!token.value) return;
  try {
    const info = await api.health();
    ready.value = info.db === "ok" ? "后端正常" : "数据库不可用";
  } catch {
    ready.value = "后端未就绪";
  }
});
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <span class="logo">TDP</span>
        <span class="title">测试数据管理平台</span>
        <el-tag v-if="ready" size="small" :type="ready === '后端正常' ? 'success' : 'warning'" effect="plain">
          {{ ready }}
        </el-tag>
      </div>
      <div v-if="token" class="who">
        <span class="user">{{ username }}</span>
        <el-tag size="small" type="info" effect="plain">{{ role }}</el-tag>
        <el-button link type="primary" @click="logout">退出</el-button>
      </div>
    </header>

    <Login v-if="!token" @login="onLogin" />

    <main v-else class="main">
      <nav class="side">
        <div
          v-for="item in tabs"
          :key="item.key"
          class="nav-item"
          :class="{ active: active === item.key }"
          @click="active = item.key"
        >
          {{ item.label }}
        </div>
      </nav>
      <section class="content">
        <component :is="view" :role="role" />
      </section>
    </main>
  </div>
</template>

<style>
* { box-sizing: border-box; }
body { margin: 0; background: #f5f7fa; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }
.shell { min-height: 100vh; }
.topbar {
  height: 56px; display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px; background: #1f3a5f; color: #fff;
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo { font-weight: 700; letter-spacing: 1px; background: #fff; color: #1f3a5f; padding: 2px 8px; border-radius: 4px; }
.title { font-size: 16px; }
.who { display: flex; align-items: center; gap: 10px; }
.who .user { font-size: 13px; opacity: 0.9; }
.who :deep(.el-button) { color: #fff; }
.main { display: flex; gap: 16px; padding: 16px; align-items: flex-start; }
.side { width: 148px; background: #fff; border-radius: 8px; padding: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.nav-item { padding: 10px 12px; border-radius: 6px; cursor: pointer; font-size: 14px; color: #303133; }
.nav-item:hover { background: #f0f2f5; }
.nav-item.active { background: #1f3a5f; color: #fff; }
.content { flex: 1; min-width: 0; background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
.page-head { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
.page-title { font-size: 15px; font-weight: 600; color: #1f3a5f; margin-right: 6px; }
.tip { color: #909399; font-size: 12px; }
</style>
