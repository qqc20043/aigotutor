/**
 * axios 客户端：统一认证、自动刷新 token、错误提示。
 *
 * 刷新策略：access token 短命（默认 2 小时）。遇到 401 时先用 refresh token
 * 换新的一对 token 并重放原请求，避免用户做到一半被踢回登录页。
 * 只有刷新本身也失败（refresh 过期 / token 版本失效）才真正登出。
 */
import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

const TOKEN_KEY = 'aigotutor_token'
const REFRESH_KEY = 'aigotutor_refresh'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function getRefreshToken(): string {
  return localStorage.getItem(REFRESH_KEY) || ''
}

export function setToken(token: string, refresh?: string): void {
  localStorage.setItem(TOKEN_KEY, token)
  if (refresh !== undefined) {
    localStorage.setItem(REFRESH_KEY, refresh)
  }
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 并发 401 时只发起一次刷新，其余请求排队等结果 */
let refreshing: Promise<string | null> | null = null

async function doRefresh(): Promise<string | null> {
  const refresh = getRefreshToken()
  if (!refresh) return null
  try {
    // 用裸 axios，避免走进本拦截器造成递归刷新
    const resp = await axios.post('/api/auth/refresh', { refresh_token: refresh })
    const access = resp.data?.access_token as string | undefined
    const newRefresh = resp.data?.refresh_token as string | undefined
    if (!access) return null
    setToken(access, newRefresh)
    return access
  } catch {
    return null
  }
}

function handleUnauthorized(): void {
  clearToken()
  if (window.location.hash !== '#/login') {
    ElMessage.warning('登录已过期，请重新登录')
    window.location.hash = '#/login'
  }
}

http.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError<{ detail?: string }>) => {
    const status = error.response?.status
    const original = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined

    // 401：尝试刷新一次并重放原请求
    if (status === 401 && original && !original._retried && getRefreshToken()) {
      original._retried = true
      if (!refreshing) {
        refreshing = doRefresh().finally(() => {
          refreshing = null
        })
      }
      const newToken = await refreshing
      if (newToken) {
        original.headers = original.headers || {}
        original.headers.Authorization = `Bearer ${newToken}`
        return http.request(original)
      }
    }

    const detail = error.response?.data?.detail
    if (status === 401) {
      handleUnauthorized()
    } else if (status === 404) {
      ElMessage.error(detail || '资源不存在')
    } else if (status === 422) {
      ElMessage.error(detail || '请求参数不合法')
    } else if (status === 429) {
      ElMessage.warning(detail || '请求过于频繁，请稍后重试')
    } else if (status && status >= 500) {
      ElMessage.error(detail ? `服务端错误：${detail}` : '服务端错误，请查看后端日志')
    } else if (error.code === 'ECONNABORTED') {
      ElMessage.error('请求超时')
    } else {
      ElMessage.error(detail || error.message || '请求失败')
    }
    return Promise.reject(error)
  },
)

/** 取后端返回的错误文本，供需要自行处理的场景使用 */
export function errorText(error: unknown): string {
  const e = error as AxiosError<{ detail?: string }>
  return e?.response?.data?.detail || e?.message || '未知错误'
}
