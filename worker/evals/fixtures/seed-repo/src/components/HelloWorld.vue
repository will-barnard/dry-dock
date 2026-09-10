<template>
  <section class="hello">
    <h1>{{ greeting }}</h1>
    <p>{{ subtitle }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ name: string }>()

const greeting = computed(() => `Hello, ${props.name}`)
const subtitle = 'Seed component for the dry-dock eval harness.'
</script>

<style scoped>
.hello {
  padding: 1rem 0;
}
</style>
