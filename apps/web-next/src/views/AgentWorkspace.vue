<script setup lang="ts">
import { useTaskStore } from "../stores/task";

const task = useTaskStore();
</script>

<template>
  <main class="workspace">
    <NCard title="Company Agent" size="large">
      <p>Vue 3 migration workspace. The legacy React application remains active during migration.</p>
      <NTag type="info">{{ task.state }}</NTag>
    </NCard>
  </main>
</template>
