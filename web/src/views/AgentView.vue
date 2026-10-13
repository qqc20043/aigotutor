<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { agentApi } from '@/api'
import { streamAgent } from '@/api/stream'
import type { AgentInfo, AgentRunResult } from '@/api/types'

const loading = ref(false)
const agents = ref<AgentInfo[]>([])
const coreAgents = ref<string[]>([])
const stats = ref<Record<string, any> | null>(null)
const statsDays = ref(7)

/** 每个 Agent 的示例输入（让页面上手即用） */
const SAMPLES: Record<string, Record<string, unknown>> = {
  judge: {
    verdict: 'TLE',
    code: 'for i in range(10**9):\n    pass\n',
    title: '两数之和',
    statement: '给定两个整数输出它们的和',
  },
  recommend: {
    candidates: [
      { problem_id: 'demo-1', title: '区间合并', difficulty: '普及-', difficulty_score: 45, tags: ['排序', '贪心'] },
      { problem_id: 'demo-2', title: '01 背包', difficulty: '普及/提高-', difficulty_score: 62, tags: ['动态规划'] },
    ],
    mastery: { 排序: 30, 动态规划: 55 },
    recent_problem_ids: [],
    target_score: 55,
  },
  solution: {
    title: '区间合并',
    statement: '给定 n 个闭区间，合并所有重叠区间并输出。n <= 10^5',
    tags: ['排序', '贪心'],
    language: 'python',
  },
  mastery: {
    tag_stats: {
      排序: { attempts: 10, ac_count: 8 },
      动态规划: { attempts: 10, ac_count: 2 },
      贪心: { attempts: 6, ac_count: 4 },
    },
    avg_difficulty_score: 55,
  },
  roadmap: {
    tags: [
      { id: '1', name: '模拟', prereq_ids: '[]' },
      { id: '2', name: '排序', prereq_ids: '[]' },
      { id: '3', name: '动态规划', prereq_ids: '["2"]' },
    ],
    mastery: { 模拟: 70, 排序: 45, 动态规划: 20 },
    weeks: 6,
  },
  difficulty: {
    title: '区间合并',
    statement: '给定 n 个闭区间，合并所有重叠区间。n <= 10^5',
    tags: ['排序', '贪心'],
    statement_len: 60,
    tag_count: 2,
    algo_depth: 3,
    constraint_tight: true,
  },
}

const form = reactive({
  agent: 'judge',
  payloadText: JSON.stringify(SAMPLES.judge, null, 2),
  useCache: true,
})

const result = ref<AgentRunResult | null>(null)
const running = ref(false)

/** 流式 */
const streaming = ref(false)
const streamText = ref('')
const streamFields = ref<Array<{ key: string; value: unknown }>>([])
const streamStartedAt = ref(0)
const firstTokenMs = ref<number | null>(null)
const streamEndedAt = ref(0)
let abortStream: (() => void) | null = null

/** 任务 */
const tasks = ref<Array<Record<string, any>>>([])
const cancelingId = ref('')

const payloadValid = computed(() => {
  try {
    JSON.parse(form.payloadText)
    return true
  } catch {
    return false
  }
})

const elapsedMs = computed(() => {
  if (!streamStartedAt.value) return 0
  const end = streamEndedAt.value || performance.now()
  return Math.round(end - streamStartedAt.value)
})

const agentStats = computed(() => {
  const by = (stats.value?.db?.by_agent || {}) as Record<string, { calls: number; degraded: number; failed: number }>
  return Object.entries(by).map(([name, v]) => ({ name, ...v }))
})

async function load() {
  loading.value = true
  try {
    const [list, st, tk] = await Promise.allSettled([
      agentApi.list(),
      agentApi.stats(statsDays.value),
      agentApi.tasks(),
    ])
    if (list.status === 'fulfilled') {
      agents.value = list.value.agents
      coreAgents.value = list.value.core_agents
    }
    if (st.status === 'fulfilled') stats.value = st.value
    if (tk.status === 'fulfilled') tasks.value = tk.value.runtime
  } finally {
    loading.value = false
  }
}

function pickAgent(name: string) {
  form.agent = name
  form.payloadText = JSON.stringify(SAMPLES[name] || {}, null, 2)
  result.value = null
  streamText.value = ''
  streamFields.value = []
}

async function runOnce() {
  if (!payloadValid.value) {
    ElMessage.warning('payload 不是合法 JSON')
    return
  }
  running.value = true
  try {
    result.value = await agentApi.run(form.agent, JSON.parse(form.payloadText), form.useCache)
    const r = result.value
    ElMessage.success(
      r.cached
        ? '命中缓存（tokens=0，未消耗额度）'
        : `完成：${r.status} · ${r.latency_ms}ms · ${r.tokens} tokens`,
    )
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    running.value = false
  }
}

function runStream() {
  if (!payloadValid.value) {
    ElMessage.warning('payload 不是合法 JSON')
    return
  }
  if (streaming.value) {
    abortStream?.()
    streaming.value = false
    return
  }

  streaming.value = true
  streamText.value = ''
  streamFields.value = []
  firstTokenMs.value = null
  streamEndedAt.value = 0
  streamStartedAt.value = performance.now()

  abortStream = streamAgent(
    { agent: form.agent, payload: JSON.parse(form.payloadText), use_cache: form.useCache },
    {
      onStart: () => {
        streamStartedAt.value = performance.now()
      },
      onToken: (t) => {
        if (firstTokenMs.value === null) {
          // 首 token 延迟：流式体验的关键指标
          firstTokenMs.value = Math.round(performance.now() - streamStartedAt.value)
        }
        streamText.value += t
      },
      onField: (key, value) => {
        const idx = streamFields.value.findIndex((f) => f.key === key)
        if (idx >= 0) streamFields.value[idx].value = value
        else streamFields.value.push({ key, value })
      },
      onDone: (res) => {
        streamEndedAt.value = performance.now()
        result.value = res as unknown as AgentRunResult
        streaming.value = false
        ElMessage.success(`流式完成，总耗时 ${elapsedMs.value}ms`)
      },
      onDegraded: (reason) => ElMessage.warning(`已降级到规则引擎：${reason}`),
      onError: (m) => {
        streaming.value = false
        ElMessage.error(`流式失败：${m}`)
      },
      onClose: () => {
        streaming.value = false
        if (!streamEndedAt.value) streamEndedAt.value = performance.now()
      },
    },
  )
}

async function cancelTask(id: string) {
  cancelingId.value = id
  try {
    const r = await agentApi.cancelTask(id)
    ElMessage.info(r?.ok ? '已请求中断' : '任务已结束')
    await load()
  } finally {
    cancelingId.value = ''
  }
}

async function clearCache() {
  await agentApi.clearCache()
  ElMessage.success('缓存已清空')
  await load()
}

function fmtValue(v: unknown): string {
  if (typeof v === 'string') return v
  return JSON.stringify(v, null, 2)
}

onMounted(load)
onBeforeUnmount(() => abortStream?.())
</script>

<template>
  <div v-loading="loading">
    <!-- Agent 卡片 -->
    <el-row :gutter="12">
      <el-col v-for="a in agents" :key="a.name" :xs="24" :sm="12" :md="8" :lg="4">
        <el-card
          shadow="hover"
          class="agent-card"
          :class="{ active: form.agent === a.name }"
          @click="pickAgent(a.name)"
        >
          <div class="agent-name">
            {{ a.name }}
            <el-tag v-if="coreAgents.includes(a.name)" size="small" type="success" effect="plain">
              核心
            </el-tag>
          </div>
          <div class="stat-label mono" style="font-size: 11px">{{ a.schema }}</div>
          <div style="margin-top: 8px">
            <el-tag v-if="a.has_rule_fallback" size="small" type="warning" effect="plain">
              有规则兜底
            </el-tag>
            <el-tag v-if="a.cacheable" size="small" type="info" effect="plain" style="margin-left: 4px">
              可缓存
            </el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <!-- 左：输入 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>输入（{{ form.agent }}）</span>
              <el-tag size="small" effect="plain">{{ payloadValid ? 'JSON 合法' : 'JSON 无效' }}</el-tag>
              <div class="flex-spacer" />
              <el-checkbox v-model="form.useCache" size="small">启用缓存</el-checkbox>
            </div>
          </template>

          <el-input
            v-model="form.payloadText"
            type="textarea"
            :rows="18"
            class="mono"
            spellcheck="false"
          />

          <div class="toolbar" style="margin-top: 12px; margin-bottom: 0">
            <el-button type="primary" :loading="running" @click="runOnce">
              <el-icon><VideoPlay /></el-icon> 直接执行
            </el-button>
            <el-button type="success" @click="runStream">
              <el-icon><Promotion /></el-icon> {{ streaming ? '停止' : '流式执行' }}
            </el-button>
            <el-button @click="pickAgent(form.agent)">恢复示例</el-button>
          </div>
        </el-card>
      </el-col>

      <!-- 右：输出 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>输出</span>
              <template v-if="streaming || streamText">
                <el-tag size="small" type="success" effect="plain">流式</el-tag>
                <el-tag v-if="firstTokenMs !== null" size="small" effect="plain">
                  首 token {{ firstTokenMs }}ms
                </el-tag>
                <el-tag size="small" effect="plain">总 {{ elapsedMs }}ms</el-tag>
              </template>
            </div>
          </template>

          <div v-if="streaming || streamText" class="stream-box">
            {{ streamText }}<span v-if="streaming" class="stream-cursor" />
          </div>

          <template v-if="streamFields.length">
            <div class="stat-label" style="margin: 10px 0 6px">
              增量字段（每个字段闭合即推送，共 {{ streamFields.length }} 个）
            </div>
            <el-collapse>
              <el-collapse-item v-for="f in streamFields" :key="f.key" :name="f.key">
                <template #title><span class="mono">{{ f.key }}</span></template>
                <pre class="code-block">{{ fmtValue(f.value) }}</pre>
              </el-collapse-item>
            </el-collapse>
          </template>

          <template v-if="result && !streaming">
            <el-divider content-position="left">最终结构化结果</el-divider>
            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px">
              <el-tag :type="result.degraded ? 'warning' : 'success'" effect="plain">
                {{ result.status }}
              </el-tag>
              <el-tag size="small" effect="plain">confidence {{ (result.confidence * 100).toFixed(0) }}%</el-tag>
              <el-tag size="small" effect="plain">{{ result.latency_ms }}ms</el-tag>
              <el-tag size="small" effect="plain">{{ result.tokens }} tokens</el-tag>
              <el-tag v-if="result.cached" size="small" type="info" effect="plain">命中缓存</el-tag>
            </div>
            <el-alert v-if="result.reason" type="warning" :closable="false" style="margin-bottom: 10px">
              {{ result.reason }}
            </el-alert>
            <pre class="code-block">{{ JSON.stringify(result.data, null, 2) }}</pre>
          </template>

          <div v-if="!result && !streamText && !streaming" class="empty-hint">
            点击"直接执行"或"流式执行"查看输出
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 运行统计 -->
    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>Agent 运行统计</span>
              <el-select v-model="statsDays" size="small" style="width: 110px" @change="load">
                <el-option :value="1" label="近 1 天" />
                <el-option :value="7" label="近 7 天" />
                <el-option :value="30" label="近 30 天" />
              </el-select>
              <div class="flex-spacer" />
              <el-button size="small" @click="clearCache">清空缓存</el-button>
            </div>
          </template>

          <el-row :gutter="10">
            <el-col :span="6">
              <el-statistic title="调用数" :value="stats?.db?.total_calls ?? 0" />
            </el-col>
            <el-col :span="6">
              <el-statistic
                title="降级率"
                :value="((stats?.db?.degraded_rate ?? 0) * 100).toFixed(1)"
                suffix="%"
              />
            </el-col>
            <el-col :span="6">
              <el-statistic
                title="缓存命中"
                :value="((stats?.cache?.hit_rate ?? 0) * 100).toFixed(0)"
                suffix="%"
              />
            </el-col>
            <el-col :span="6">
              <el-statistic title="P95 延迟" :value="stats?.db?.latency_p95_ms ?? 0" suffix="ms" />
            </el-col>
          </el-row>

          <el-table :data="agentStats" size="small" style="margin-top: 14px">
            <el-table-column prop="name" label="Agent" />
            <el-table-column prop="calls" label="调用" width="80" />
            <el-table-column label="降级" width="80">
              <template #default="{ row }">
                <el-tag v-if="row.degraded" size="small" type="warning" effect="plain">
                  {{ row.degraded }}
                </el-tag>
                <span v-else class="stat-label">0</span>
              </template>
            </el-table-column>
            <el-table-column label="失败" width="80">
              <template #default="{ row }">
                <el-tag v-if="row.failed" size="small" type="danger" effect="plain">
                  {{ row.failed }}
                </el-tag>
                <span v-else class="stat-label">0</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <!-- 任务列表 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>后台任务</span>
              <el-tag size="small" effect="plain">
                并发上限 {{ stats?.runtime?.concurrency_limit ?? '-' }}
              </el-tag>
              <div class="flex-spacer" />
              <el-button size="small" @click="load">刷新</el-button>
            </div>
          </template>

          <el-table :data="tasks" size="small" max-height="360">
            <el-table-column label="任务" min-width="150">
              <template #default="{ row }">
                <span class="mono" style="font-size: 11px">{{ row.task_id.slice(0, 16) }}…</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="
                    row.status === 'done'
                      ? 'success'
                      : row.status === 'failed'
                        ? 'danger'
                        : row.status === 'running'
                          ? 'primary'
                          : 'info'
                  "
                  effect="plain"
                >
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="进度" width="120">
              <template #default="{ row }">
                <el-progress :percentage="Math.round((row.progress || 0) * 100)" :stroke-width="8" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button
                  v-if="['pending', 'running'].includes(row.status)"
                  link
                  type="danger"
                  size="small"
                  :loading="cancelingId === row.task_id"
                  @click="cancelTask(row.task_id)"
                >
                  中断
                </el-button>
                <span v-else class="stat-label">-</span>
              </template>
            </el-table-column>
            <template #empty>
              <div class="empty-hint">暂无任务</div>
            </template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.agent-card {
  cursor: pointer;
  margin-bottom: 12px;
  transition: all 0.15s;
}

.agent-card.active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.agent-name {
  font-weight: 600;
  font-size: 15px;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
</style>
