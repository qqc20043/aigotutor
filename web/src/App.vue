<script setup lang="ts">
import { onMounted } from 'vue'
import { useUserStore } from '@/stores/user'

const user = useUserStore()

onMounted(() => {
  if (user.isLoggedIn) void user.fetchMe()
})
</script>

<template>
  <router-view />
</template>
