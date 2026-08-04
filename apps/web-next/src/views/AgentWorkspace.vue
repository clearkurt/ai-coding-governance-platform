<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { useRouter } from "vue-router";
import { useSessionStore } from "../stores/session";
import { useTaskStore } from "../stores/task";

const session = useSessionStore();
const task = useTaskStore();
const router = useRouter();
const deviceId = ref("");
const projectId = ref("");
const conversationId = ref("");
const prompt = ref("");
const submitting = ref(false);
const formError = ref("");
const canSubmit = computed(
  () =>
    deviceId.value.trim() &&
    projectId.value.trim() &&
    conversationId.value.trim() &&
    prompt.value.trim(),
);
const statusType = computed(() => {
  if (task.state === "failed") return "error";
  if (task.state === "completed") return "success";
  if (task.state === "waiting_approval") return "warning";
  return "info";
});

async function start() {
  submitting.value = true;
  formError.value = "";
  try {
    await task.create({
      deviceId: deviceId.value.trim(),
      projectId: projectId.value.trim(),
      conversationId: conversationId.value.trim(),
      prompt: prompt.value.trim(),
    });
  } catch (reason) {
    task.state = "failed";
    formError.value = reason instanceof Error ? reason.message : "任务创建失败";
  } finally {
    submitting.value = false;
  }
}

async function logout() {
  await session.logout();
  task.disconnect();
  await router.push("/login");
}

onBeforeUnmount(() => task.disconnect());
</script>

<template>
  <NLayout class="app-layout">
    <NLayoutHeader bordered class="topbar">
      <div><strong>Company Agent</strong><span class="muted">Codex + DeepSeek</span></div>
      <div class="topbar-user">
        <span>{{ session.user?.email }}</span>
        <NButton text @click="logout">退出</NButton>
      </div>
    </NLayoutHeader>
    <NLayoutContent class="workspace">
      <section class="task-grid">
        <NCard title="新任务" size="small">
          <NForm @submit.prevent="start">
            <NFormItem label="设备 ID">
              <NInput v-model:value="deviceId" placeholder="已配对设备 UUID" />
            </NFormItem>
            <NFormItem label="项目 ID">
              <NInput v-model:value="projectId" placeholder="已授权项目 UUID" />
            </NFormItem>
            <NFormItem label="会话 ID">
              <NInput v-model:value="conversationId" placeholder="会话 UUID" />
            </NFormItem>
            <NFormItem label="需求">
              <NInput
                v-model:value="prompt"
                type="textarea"
                :autosize="{ minRows: 7, maxRows: 16 }"
              />
            </NFormItem>
            <NAlert v-if="formError" type="error" class="form-alert">{{ formError }}</NAlert>
            <NButton
              type="primary"
              attr-type="submit"
              :loading="submitting"
              :disabled="!canSubmit"
            >
              开始执行
            </NButton>
            <NButton
              v-if="task.taskId && !['completed', 'failed', 'cancelled'].includes(task.state)"
              class="cancel-button"
              @click="task.cancel"
            >
              取消
            </NButton>
            <NButton
              v-if="task.taskId && ['completed', 'failed', 'cancelled'].includes(task.state)"
              class="cancel-button"
              @click="task.rollback"
            >
              回滚本次修改
            </NButton>
          </NForm>
        </NCard>
        <NCard title="执行状态" size="small">
          <div class="status-row">
            <NTag :type="statusType">{{ task.state }}</NTag>
            <code v-if="task.taskId">{{ task.taskId }}</code>
          </div>
          <NAlert v-if="task.error" type="warning" class="form-alert">{{ task.error }}</NAlert>
          <NAlert v-if="task.rollbackStatus" type="info" class="form-alert">
            回滚状态：{{ task.rollbackStatus }}
          </NAlert>
          <section
            v-if="task.approvals.some((item) => item.status === 'pending')"
            class="approval-panel"
          >
            <strong>等待审批</strong>
            <div
              v-for="approval in task.approvals.filter((item) => item.status === 'pending')"
              :key="approval.id"
              class="approval-row"
            >
              <code>{{ approval.provider_item_id }}</code>
              <NButton size="small" type="primary" @click="task.decide(approval.id, 'approved')">
                批准
              </NButton>
              <NButton size="small" type="error" @click="task.decide(approval.id, 'rejected')">
                拒绝
              </NButton>
            </div>
          </section>
          <NEmpty v-if="!task.events.length" description="尚无任务事件" />
          <NTimeline v-else>
            <NTimelineItem
              v-for="event in task.events"
              :key="event.sequence"
              :title="event.type"
              :time="`#${event.sequence}`"
            >
              <pre>{{ JSON.stringify(event.payload, null, 2) }}</pre>
            </NTimelineItem>
          </NTimeline>
        </NCard>
      </section>
    </NLayoutContent>
  </NLayout>
</template>
