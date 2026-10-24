import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { getToken } from '@/api/http'
import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '概览', icon: 'Odometer' },
      },
      {
        path: 'problems',
        name: 'problems',
        component: () => import('@/views/ProblemListView.vue'),
        meta: { title: '题库', icon: 'List' },
      },
      {
        path: 'problems/:id',
        name: 'problem-detail',
        component: () => import('@/views/ProblemDetailView.vue'),
        meta: { title: '做题', hidden: true },
      },
      {
        path: 'chat/:problemId?',
        name: 'chat',
        component: () => import('@/views/ChatView.vue'),
        meta: { title: '对话辅导', icon: 'ChatDotRound' },
      },
      {
        path: 'interview',
        name: 'interview',
        component: () => import('@/views/InterviewView.vue'),
        meta: { title: '模拟面试', icon: 'UserFilled' },
      },
      {
        path: 'mastery',
        name: 'mastery',
        component: () => import('@/views/MasteryView.vue'),
        meta: { title: '掌握度画像', icon: 'TrendCharts' },
      },
      {
        path: 'review',
        name: 'review',
        component: () => import('@/views/ReviewView.vue'),
        meta: { title: '复习队列', icon: 'AlarmClock' },
      },
      {
        path: 'wrong-book',
        name: 'wrong-book',
        component: () => import('@/views/WrongBookView.vue'),
        meta: { title: '错题本', icon: 'Collection' },
      },
      {
        path: 'checkin',
        name: 'checkin',
        component: () => import('@/views/CheckinView.vue'),
        meta: { title: '打卡与排行', icon: 'Calendar' },
      },
      {
        path: 'report',
        name: 'report',
        component: () => import('@/views/ReportView.vue'),
        meta: { title: '学习报告', icon: 'DataAnalysis' },
      },
      {
        path: 'import',
        name: 'import',
        component: () => import('@/views/ImportView.vue'),
        meta: { title: '题面导入', icon: 'Upload' },
      },
      {
        path: 'agent',
        name: 'agent',
        component: () => import('@/views/AgentView.vue'),
        meta: { title: 'Agent 控制台', icon: 'MagicStick' },
      },
      {
        path: 'submissions',
        name: 'submissions',
        component: () => import('@/views/SubmissionListView.vue'),
        meta: { title: '提交记录', icon: 'Document' },
      },
      {
        path: 'admin',
        name: 'admin',
        component: () => import('@/views/AdminView.vue'),
        meta: { title: '管理后台', icon: 'Setting', requiresAdmin: true },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置', icon: 'Tools' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  // hash 模式：后端静态托管时不需要额外配置 SPA 回退规则
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const loggedIn = Boolean(getToken())
  if (!to.meta.public && !loggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && loggedIn) {
    return { name: 'dashboard' }
  }
  // 管理后台：路由层直接拦截（后端 AdminDep 仍是最终防线，这里只是
  // 不让普通用户进到"全 403 的空页面"——体验问题而非安全问题）
  if (to.meta.requiresAdmin && loggedIn) {
    const user = useUserStore()
    if (!user.user) await user.fetchMe()
    if (!user.isAdmin) return { name: 'dashboard' }
  }
  return true
})

// 部署后用户仍持有旧 index.html：点菜单时按需加载的页面 chunk 请求旧
// hash 文件名 -> 404 -> 动态 import 失败 -> 导航静默中止（表现为"点了没
// 反应"，2026-10-24 实测复现两次）。兜底：整页重载一次拿新 index.html
// 即愈；afterEach 里清标记，保证后续部署还能各自恢复一次，不会死循环。
router.onError((error, to) => {
  const msg = String((error as Error)?.message || "")
  const chunkFail =
    msg.includes("Failed to fetch dynamically imported module") ||
    msg.includes("Importing a module script failed") ||
    msg.includes("error loading dynamically imported module") ||
    msg.includes("Loading chunk")
  if (chunkFail && !sessionStorage.getItem("aigotutor_chunk_reload")) {
    sessionStorage.setItem("aigotutor_chunk_reload", "1")
    window.location.href = to?.fullPath || "/"
  }
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || ''
  document.title = title ? `${title} · AIgoTutor` : 'AIgoTutor'
  sessionStorage.removeItem("aigotutor_chunk_reload")
})

export default router
