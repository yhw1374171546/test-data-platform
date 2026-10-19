<script setup>
import { reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api, session } from "../api";

const emit = defineEmits(["login"]);
const form = reactive({ username: "admin", password: "Admin@12345" });
const loading = ref(false);

async function submit() {
  loading.value = true;
  try {
    const data = await api.login(form.username, form.password);
    session.save(data);
    emit("login", data);
  } catch (err) {
    ElMessage.error(err.friendly || "登录失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="card">
      <h2>登录</h2>
      <p class="hint">演示账号：admin / Admin@12345（可写）、tester / Tester@12345、viewer / Viewer@12345</p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="账号">
          <el-input v-model="form.username" placeholder="admin" />
        </el-form-item>
        <el-form-item label="口令">
          <el-input v-model="form.password" type="password" show-password @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">登录</el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-wrap { display: flex; justify-content: center; padding-top: 9vh; }
.card { width: 380px; background: #fff; border-radius: 10px; padding: 26px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }
h2 { margin: 0 0 6px; color: #1f3a5f; }
.hint { font-size: 12px; color: #909399; margin: 0 0 16px; line-height: 1.6; }
</style>
