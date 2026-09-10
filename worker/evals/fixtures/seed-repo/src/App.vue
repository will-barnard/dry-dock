<template>
  <div class="app">
    <nav>
      <RouterLink to="/">Home</RouterLink>
    </nav>
    <RouterView />
  </div>
</template>

<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<style scoped>
.app {
  font-family: system-ui, sans-serif;
  max-width: 960px;
  margin: 0 auto;
}
</style>
