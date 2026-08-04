<script setup lang="ts">
import { RouterView } from "vue-router";
</script>

<template>
  <NConfigProvider>
    <NGlobalStyle />
    <RouterView />
  </NConfigProvider>
</template>
