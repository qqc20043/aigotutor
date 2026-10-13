/** 登录态与用户信息 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi } from '@/api'
import { clearToken, getToken, setToken } from '@/api/http'
import type { UserOut } from '@/api/types'

export const useUserStore = defineStore('user', () => {
  const token = ref<string>(getToken())
  const user = ref<UserOut | null>(null)
  const loading = ref(false)

  const isLoggedIn = computed(() => Boolean(token.value))
  const displayName = computed(() => user.value?.nickname || user.value?.username || '未登录')
  const isAdmin = computed(() => user.value?.role === 'admin')

  async function login(username: string, password: string): Promise<void> {
    loading.value = true
    try {
      const out = await authApi.login({ username, password })
      token.value = out.access_token
      // 同时保存 refresh token：access 过期后由拦截器自动续期
      setToken(out.access_token, out.refresh_token)
      await fetchMe()
    } finally {
      loading.value = false
    }
  }

  async function register(username: string, password: string, nickname?: string): Promise<void> {
    loading.value = true
    try {
      const out = await authApi.register({ username, password, nickname })
      token.value = out.access_token
      setToken(out.access_token, out.refresh_token)
      await fetchMe()
    } finally {
      loading.value = false
    }
  }

  async function fetchMe(): Promise<void> {
    if (!token.value) return
    try {
      user.value = await authApi.me()
    } catch {
      // token 失效时由 http 拦截器统一处理登出
      user.value = null
    }
  }

  async function logout(allDevices = false): Promise<void> {
    try {
      await authApi.logout(allDevices)
    } catch {
      // 服务端登出失败也要清理本地状态，否则用户被卡住
    }
    token.value = ''
    user.value = null
    clearToken()
  }

  return {
    token,
    user,
    loading,
    isLoggedIn,
    isAdmin,
    displayName,
    login,
    register,
    fetchMe,
    logout,
  }
})
