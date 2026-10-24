<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { agentApi } from '@/api'
import { levelText } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const user = useUserStore()

const collapsed = ref(false)
const judgeInfo = ref<string>('')

const menuItems = computed(() =>
  [
    { path: '/dashboard', title: '概览', icon: 'Odometer' },
    { path: '/problems', title: '题库', icon: 'List' },
    { path: '/chat', title: '对话辅导', icon: 'ChatDotRound' },
    { path: '/import', title: '题面导入', icon: 'Upload' },
    { path: '/mastery', title: '掌握度画像', icon: 'TrendCharts' },
    { path: '/review', title: '复习队列', icon: 'AlarmClock' },
    { path: '/wrong-book', title: '错题本', icon: 'Collection' },
    { path: '/checkin', title: '打卡与排行', icon: 'Calendar' },
    { path: '/report', title: '学习报告', icon: 'DataAnalysis' },
    { path: '/agent', title: 'Agent 控制台', icon: 'MagicStick' },
    { path: '/submissions', title: '提交记录', icon: 'Document' },
    // 管理后台只对管理员显示（后端仍会校验，这里只是不显示无用入口）
    ...(user.isAdmin ? [{ path: '/admin', title: '管理后台', icon: 'Setting' }] : []),
    { path: '/settings', title: '设置', icon: 'Tools' },
  ],
)

const activeMenu = computed(() => {
  // 题目详情页高亮"题库"
  if (route.path.startsWith('/problems')) return '/problems'
  return route.path
})

onMounted(async () => {
  if (!user.user) await user.fetchMe()
  try {
    const info = await agentApi.stats()
    const limit = info?.runtime?.concurrency_limit ?? '-'
    judgeInfo.value = `并发上限 ${limit}`
  } catch {
    judgeInfo.value = ''
  }
})

async function handleLogout() {
  try {
    await ElMessageBox.confirm('确认退出登录？', '提示', { type: 'warning' })
  } catch {
    return
  }
  user.logout()
  void router.push('/login')
}
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside :width="collapsed ? '64px' : '210px'" class="aside">
      <div class="logo">
        <span class="logo-mark">AI</span>
        <span v-show="!collapsed" class="logo-text">goTutor</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        background-color="#1f2d3d"
        text-color="#c0c4cc"
        active-text-color="#ffffff"
        router
      >
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <el-button text :icon="collapsed ? 'Expand' : 'Fold'" @click="collapsed = !collapsed" />
        <span class="page-title">{{ route.meta.title || '' }}</span>
        <div class="flex-spacer" />
        <el-tag v-if="judgeInfo" size="small" type="info" effect="plain" style="margin-right: 12px">
          {{ judgeInfo }}
        </el-tag>
        <el-dropdown v-if="user.isLoggedIn" @command="handleLogout">
          <span class="user-chip">
            <el-avatar :size="26" style="background: #409eff">
              {{ user.displayName.slice(0, 1) }}
            </el-avatar>
            <span class="user-name">{{ user.displayName }}</span>
            <el-tag v-if="user.user" size="small" effect="plain">
              {{ user.user.level }} 级 · {{ levelText(user.user.level >= 4 ? 'L4' : 'L3') }}
            </el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">
                <el-icon><SwitchButton /></el-icon> 退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="main">
        <router-view v-slot="{ Component }">
          <component :is="Component" />
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.aside {
  background: var(--app-sidebar-bg);
  transition: width 0.2s;
  overflow-x: hidden;
}

.aside :deep(.el-menu) {
  border-right: none;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: #fff;
  font-weight: 700;
  font-size: 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.logo-mark {
  color: #409eff;
}

.header {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
  height: 60px;
}

.page-title {
  font-size: 16px;
  font-weight: 600;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.user-name {
  font-size: 14px;
  color: #303133;
}

.main {
  padding: 18px;
  overflow-y: auto;
}
</style>
