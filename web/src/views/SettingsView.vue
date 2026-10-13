<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { agentApi, authApi, dataApi, problemApi } from '@/api'
import { useUserStore } from '@/stores/user'
import { fmtDate } from '@/utils/format'
import type { JudgeBackendInfo, ModelConfig } from '@/api/types'

const user = useUserStore()

const loading = ref(false)
const saving = ref(false)
const judgeInfo = ref<JudgeBackendInfo | null>(null)
const globalStats = ref<Record<string, any> | null>(null)
const currentConfig = ref<ModelConfig | null>(null)
const healthOk = ref<boolean | null>(null)

const form = reactive({
  provider: 'mock',
  model: 'mock-1',
  base_url: '',
  api_key: '',
  temperature: 0.2,
  max_tokens: 2048,
})

const PROVIDERS = [
  { value: 'mock', label: 'mock（离线，无需 Key）', hint: '确定性输出，适合开发与演示' },
  { value: 'openai', label: 'OpenAI 兼容', hint: '兼容各类 /v1/chat/completions 网关' },
  { value: 'anthropic', label: 'Anthropic', hint: '使用 tool_use 强制结构化输出' },
  { value: 'gemini', label: 'Google Gemini', hint: '使用 responseSchema 原生结构化' },
]

const providerHint = () => PROVIDERS.find((p) => p.value === form.provider)?.hint || ''

async function load() {
  loading.value = true
  try {
    const [cfg, judge, stats, health] = await Promise.allSettled([
      authApi.getModelConfig(),
      problemApi.judgeBackend(),
      dataApi.globalStats(),
      dataApi.health(),
    ])
    if (cfg.status === 'fulfilled') {
      currentConfig.value = cfg.value
      form.provider = cfg.value.provider
      form.model = cfg.value.model
      form.base_url = cfg.value.base_url
      form.temperature = cfg.value.temperature
      form.max_tokens = cfg.value.max_tokens
    }
    if (judge.status === 'fulfilled') judgeInfo.value = judge.value
    if (stats.status === 'fulfilled') globalStats.value = stats.value
    if (health.status === 'fulfilled') healthOk.value = Boolean((health.value as any)?.db)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    await authApi.setModelConfig({
      provider: form.provider,
      model: form.model,
      base_url: form.base_url,
      api_key: form.api_key || undefined,
      temperature: form.temperature,
      max_tokens: form.max_tokens,
      purpose: 'default',
    })
    ElMessage.success('模型配置已保存（仅对当前用户生效）')
    form.api_key = ''
    await load()
  } finally {
    saving.value = false
  }
}

async function clearCache() {
  await agentApi.clearCache()
  ElMessage.success('结果缓存已清空')
  await load()
}

async function exportData() {
  const data = await dataApi.exportUser()
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `aigotutor-export-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已导出学习数据')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 8px">
              <span>模型配置</span>
              <el-tag v-if="currentConfig?.is_default" size="small" type="info" effect="plain">
                使用全局默认
              </el-tag>
              <el-tag v-else size="small" type="success" effect="plain">已自定义</el-tag>
            </div>
          </template>

          <el-form label-width="110px">
            <el-form-item label="Provider">
              <el-select v-model="form.provider" style="width: 100%">
                <el-option v-for="p in PROVIDERS" :key="p.value" :label="p.label" :value="p.value" />
              </el-select>
              <div class="stat-label" style="margin-top: 4px">{{ providerHint() }}</div>
            </el-form-item>

            <el-form-item label="模型名">
              <el-input v-model="form.model" placeholder="例如 gpt-4o-mini / claude-3-5-haiku-latest" />
            </el-form-item>

            <el-form-item label="Base URL">
              <el-input v-model="form.base_url" placeholder="留空使用官方地址；兼容网关可填自建地址" />
            </el-form-item>

            <el-form-item label="API Key">
              <el-input
                v-model="form.api_key"
                type="password"
                show-password
                :placeholder="currentConfig?.api_key_masked ? `已保存：${currentConfig.api_key_masked}（留空则不修改）` : 'mock 模式无需填写'"
              />
            </el-form-item>

            <el-form-item label="温度">
              <el-slider v-model="form.temperature" :min="0" :max="2" :step="0.1" show-input />
            </el-form-item>

            <el-form-item label="最大 Tokens">
              <el-input-number v-model="form.max_tokens" :min="64" :max="32000" :step="256" />
            </el-form-item>

            <el-form-item>
              <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
            </el-form-item>
          </el-form>

          <el-alert type="info" :closable="false">
            <div class="stat-label" style="line-height: 1.8">
              密钥对外只返回脱敏值。配置仅作用于当前用户，其他用户不受影响。
              不填 Key 时走 mock provider，可离线完整体验全链路。
            </div>
          </el-alert>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>判题后端</template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="后端">{{ judgeInfo?.backend || '-' }}</el-descriptions-item>
            <el-descriptions-item label="可用">
              <el-tag size="small" :type="judgeInfo?.available ? 'success' : 'danger'" effect="plain">
                {{ judgeInfo?.available ? '是' : '否' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="隔离强度">
              <el-tag
                size="small"
                :type="judgeInfo?.isolation === 'weak' ? 'warning' : 'success'"
                effect="plain"
              >
                {{ judgeInfo?.isolation }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="语言">
              {{ (judgeInfo?.languages || []).join(' / ') || '无' }}
            </el-descriptions-item>
            <el-descriptions-item label="内存限制方式">
              {{ judgeInfo?.memory_limit || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="超时 / 内存">
              {{ judgeInfo?.timeout_s ?? '-' }}s / {{ judgeInfo?.memory_mb ?? '-' }}MB
            </el-descriptions-item>
          </el-descriptions>

          <el-alert
            v-if="judgeInfo?.isolation === 'weak'"
            type="warning"
            :closable="false"
            style="margin-top: 12px"
          >
            <div style="line-height: 1.8">
              <b>当前为本机子进程判题，隔离强度弱。</b><br />
              适用于本地开发、内网与可信用户的练习场景。面向公网请切换
              <span class="mono">AIGOTUTOR_JUDGE_BACKEND=docker</span>。
            </div>
          </el-alert>

          <el-alert
            v-for="w in judgeInfo?.warnings || []"
            :key="w"
            type="error"
            :closable="false"
            style="margin-top: 8px"
          >
            {{ w }}
          </el-alert>

          <el-alert
            v-if="judgeInfo?.note"
            type="info"
            :closable="false"
            style="margin-top: 12px"
          >
            <div class="stat-label" style="line-height: 1.8">{{ judgeInfo.note }}</div>
          </el-alert>
        </el-card>

        <el-card class="page-card" shadow="never">
          <template #header>运行态</template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="服务健康">
              <el-tag size="small" :type="healthOk ? 'success' : 'danger'" effect="plain">
                {{ healthOk ? '正常' : '异常' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="默认 Provider">
              {{ globalStats?.provider || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="缓存条数">
              {{ globalStats?.cache?.size ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="缓存命中率">
              {{ ((globalStats?.cache?.hit_rate ?? 0) * 100).toFixed(0) }}%
            </el-descriptions-item>
            <el-descriptions-item label="任务总数">
              {{ globalStats?.tasks?.total ?? 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="并发上限">
              {{ globalStats?.tasks?.concurrency_limit ?? '-' }}
            </el-descriptions-item>
          </el-descriptions>
          <div class="toolbar" style="margin-top: 12px; margin-bottom: 0">
            <el-button size="small" @click="clearCache">清空缓存</el-button>
            <el-button size="small" @click="exportData">导出学习数据</el-button>
            <el-button size="small" @click="load">刷新</el-button>
          </div>
        </el-card>

        <el-card class="page-card" shadow="never">
          <template #header>账号</template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="用户名">{{ user.user?.username || '-' }}</el-descriptions-item>
            <el-descriptions-item label="昵称">{{ user.user?.nickname || '-' }}</el-descriptions-item>
            <el-descriptions-item label="自评等级">{{ user.user?.level ?? '-' }} / 5</el-descriptions-item>
            <el-descriptions-item label="注册时间">
              {{ user.user ? fmtDate(user.user.created_at) : '-' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
