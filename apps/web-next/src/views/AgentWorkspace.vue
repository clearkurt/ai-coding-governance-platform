<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { useSessionStore } from "../stores/session";
import { useTaskStore } from "../stores/task";
import { useResourceStore } from "../stores/resources";

const session = useSessionStore();
const task = useTaskStore();
const resources = useResourceStore();
const router = useRouter();
const deviceId = ref("");
const projectId = ref("");
const conversationId = ref("");
const prompt = ref("");
const submitting = ref(false);
const formError = ref("");
const pairingCode = ref("");
const pairingExpiresAt = ref("");
const conversationTitle = ref("");
const deviceOptions = computed(() =>
  resources.devices.map((device) => ({
    label: `${device.name}${device.online ? "（在线）" : "（离线）"}`,
    value: device.id,
  })),
);
const selectedDevice = computed(() =>
  resources.devices.find((device) => device.id === deviceId.value),
);
const projectOptions = computed(() =>
  (selectedDevice.value?.projects ?? []).map((project) => ({
    label: project.display_name,
    value: project.id,
  })),
);
const conversationOptions = computed(() =>
  resources.conversations.map((conversation) => ({
    label: conversation.title,
    value: conversation.id,
  })),
);
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

async function createConversation() {
  const title = conversationTitle.value.trim() || "新会话";
  try {
    const conversation = await resources.createConversation(title);
    conversationId.value = conversation.id;
    conversationTitle.value = "";
  } catch (reason) {
    formError.value = reason instanceof Error ? reason.message : "会话创建失败";
  }
}

async function createPairingCode() {
  try {
    const result = await resources.createPairingCode();
    pairingCode.value = result.code;
    pairingExpiresAt.value = result.expires_at;
  } catch (reason) {
    formError.value = reason instanceof Error ? reason.message : "配对码创建失败";
  }
}

watch(deviceId, () => {
  if (!selectedDevice.value?.projects.some((project) => project.id === projectId.value)) {
    projectId.value = selectedDevice.value?.projects[0]?.id ?? "";
  }
});
onMounted(async () => {
  try {
    await resources.load();
    deviceId.value = resources.devices[0]?.id ?? "";
    projectId.value = resources.devices[0]?.projects[0]?.id ?? "";
    conversationId.value = resources.conversations[0]?.id ?? "";
  } catch (reason) {
    formError.value = reason instanceof Error ? reason.message : "资源加载失败";
  }
});

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
              <NSelect
                v-model:value="deviceId"
                :options="deviceOptions"
                :loading="resources.loading"
                placeholder="选择已配对设备"
              />
            </NFormItem>
            <NFormItem label="项目">
              <NSelect v-model:value="projectId" :options="projectOptions" placeholder="选择授权项目" />
            </NFormItem>
            <NFormItem label="会话">
              <NSelect
                v-model:value="conversationId"
                :options="conversationOptions"
                placeholder="选择会话"
              />
            </NFormItem>
            <div class="inline-create">
              <NInput v-model:value="conversationTitle" placeholder="新会话标题" />
              <NButton @click="createConversation">新建会话</NButton>
            </div>
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
            <NButton class="cancel-button" @click="createPairingCode">生成设备配对码</NButton>
          </NForm>
          <NAlert v-if="pairingCode" type="success" class="pairing-code">
            配对码：<strong>{{ pairingCode }}</strong><br />有效期至 {{ pairingExpiresAt }}
          </NAlert>
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
