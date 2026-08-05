<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const router = useRouter();
const teamId = ref("");
const email = ref("");
const password = ref("");
const error = ref("");
const submitting = ref(false);

async function submit() {
  submitting.value = true;
  error.value = "";
  try {
    await session.login(teamId.value.trim(), email.value.trim(), password.value);
    await router.push("/");
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "登录失败";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="login-shell">
    <NCard title="Company Agent" class="login-card">
      <NForm @submit.prevent="submit">
        <NFormItem label="团队 ID"><NInput v-model:value="teamId" /></NFormItem>
        <NFormItem label="邮箱"><NInput v-model:value="email" /></NFormItem>
        <NFormItem label="密码">
          <NInput v-model:value="password" type="password" show-password-on="click" />
        </NFormItem>
        <NAlert v-if="error" type="error" class="form-alert">{{ error }}</NAlert>
        <NButton attr-type="submit" type="primary" block :loading="submitting">登录</NButton>
      </NForm>
    </NCard>
  </main>
</template>
