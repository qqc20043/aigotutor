<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { problemApi, submissionApi, agentApi } from '@/api'
import { streamAgent, streamTask } from '@/api/stream'
import type {
  JudgeBackendInfo,
  ProblemDetail,
  SimilarProblem,
  Submission,
  SubmitResult,
  TaskSnapshot,
  TestCase,
} from '@/api/types'
import { difficultyType, fmtRelative, verdictText, verdictType } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const problemId = computed(() => String(route.params.id || ''))

const loading = ref(false)
const problem = ref<ProblemDetail | null>(null)
const judgeInfo = ref<JudgeBackendInfo | null>(null)
const testcases = ref<TestCase[]>([])
const history = ref<Submission[]>([])

const LANG_TEMPLATES: Record<string, string> = {
  python: 'import sys\n\n\ndef main() -> None:\n    data = sys.stdin.read().split()\n    if not data:\n        return\n    # TODO: 实现你的算法\n    print(0)\n\n\nif __name__ == "__main__":\n    main()\n',
  cpp: '#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    ios::sync_with_stdio(false);\n    cin.tie(nullptr);\n    // TODO: 实现你的算法\n    cout << 0 << \'\\n\';\n    return 0;\n}\n',
  java: 'import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner sc = new Scanner(System.in);\n        // TODO: 实现你的算法\n        System.out.println(0);\n    }\n}\n',
}

const code = reactive({
  language: 'python',
  source: LANG_TEMPLATES.python,
  judgeMode: 'auto',
  runAgents: true,
})

const submitting = ref(false)
const result = ref<SubmitResult | null>(null)
const taskProgress = ref<TaskSnapshot | null>(null)
const pipelineAgents = ref<Array<{ name: string; status: string; degraded: boolean; confidence: number; latency_ms: number }>>([])

/** 单 Agent 流式面板 */
const streaming = ref(false)
const streamText = ref('')
const streamFields = ref<Array<{ key: string; value: unknown }>>([])
const streamAgentName = ref('solution')
let abortStream: (() => void) | null = null
let abortTask: (() => void) | null = null

/** 渐进式提示面板（强度由该题知识点掌握度自动决定） */
const hintLoading = ref(false)
const hint = ref<{
  strength: string
  hints: string[]
  key_insight: string
  next_step: string
  strength_reason: string
  degraded?: boolean
  memory_tokens?: number
} | null>(null)

/** 强度档位展示：让用户明白"为什么给我这个强度的提示" */
const HINT_STRENGTH_META: Record<string, { label: string; type: 'info' | 'warning' | 'danger' }> = {
  direction: { label: '方向提示', type: 'info' },
  key_observation: { label: '关键观察', type: 'warning' },
  pseudocode: { label: '步骤骨架', type: 'danger' },
}

const hintMeta = computed(() =>
  hint.value ? HINT_STRENGTH_META[hint.value.strength] || HINT_STRENGTH_META.key_observation : null,
)

async function loadHint() {
  hintLoading.value = true
  try {
    const res = await problemApi.hint(problemId.value, {
      language: code.language,
      current_code: code.source,
    })
    hint.value = (res.data || null) as typeof hint.value
    if (!hint.value) ElMessage.warning('未获取到提示')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取提示失败')
  } finally {
    hintLoading.value = false
  }
}

const passedRatio = computed(() => {
  const s = result.value?.submission
  if (!s || !s.total_cases) return 0
  return Math.round((s.passed_cases / s.total_cases) * 100)
})

const detailCases = computed<Array<Record<string, any>>>(() => {
  const detail = result.value?.judge_detail || {}
  return Array.isArray(detail.cases) ? detail.cases : []
})

async function loadProblem() {
  loading.value = true
  try {
    const [p, tc, hist, judge] = await Promise.allSettled([
      problemApi.detail(problemId.value),
      problemApi.testcases(problemId.value),
      submissionApi.list({ problem_id: problemId.value, page: 1, size: 10 }),
      problemApi.judgeBackend(),
    ])
    if (p.status === 'fulfilled') problem.value = p.value
    if (tc.status === 'fulfilled') testcases.value = tc.value.items
    if (hist.status === 'fulfilled') history.value = hist.value.items
    if (judge.status === 'fulfilled') judgeInfo.value = judge.value as unknown as JudgeBackendInfo

    // 没有测试用例时默认退回调用方判定，避免用户提交后拿到 IE
    if (testcases.value.length === 0) {
      code.judgeMode = 'caller'
    }
  } finally {
    loading.value = false
  }
}

function switchLanguage(lang: string) {
  code.language = lang
  const tpl = LANG_TEMPLATES[lang]
  if (tpl && (!code.source.trim() || code.source === LANG_TEMPLATES[Object.keys(LANG_TEMPLATES).find((k) => LANG_TEMPLATES[k] === code.source) || 'python'])) {
    code.source = tpl
  }
}

function resetCode() {
  code.source = LANG_TEMPLATES[code.language] || ''
}

async function submit() {
  if (!code.source.trim()) {
    ElMessage.warning('请先编写代码')
    return
  }
  if (testcases.value.length === 0 && code.judgeMode === 'sandbox') {
    ElMessage.warning('本题未配置测试用例，沙箱无法判题；已改用调用方判定')
    code.judgeMode = 'caller'
  }

  submitting.value = true
  result.value = null
  pipelineAgents.value = []
  taskProgress.value = null
  // 幂等键：避免网络重试或连点产生重复提交记录
  const idemKey = `p-${problemId.value}-${Date.now()}`
  try {
    const out = await submissionApi.submit(
      {
        problem_id: problemId.value,
        code: code.source,
        language: code.language,
        judge_mode: code.judgeMode,
        verdict: 'WA',
        run_agents: code.runAgents,
        dedupe: true,
      },
      idemKey,
    )
    result.value = out
    if (out.deduplicated) {
      ElMessage.info(out.reason || '命中重复提交，已复用记录')
    } else {
      ElMessage.success(`判题完成：${verdictText(out.verdict)}`)
    }
    void loadHistory()
  } catch {
    /* 拦截器已提示 */
  } finally {
    submitting.value = false
  }
}

async function loadHistory() {
  try {
    const h = await submissionApi.list({ problem_id: problemId.value, page: 1, size: 10 })
    history.value = h.items
  } catch {
    /* ignore */
  }
}

/** 跑完整 Agent 链路（后台任务 + SSE 进度） */
async function runPipeline() {
  if (!result.value) {
    ElMessage.warning('请先提交一次，完成任务链路需要判题结果')
    return
  }
  try {
    const pipeline = await agentApi.pipeline({      submission: {
        problem_id: problemId.value,
        code: code.source,
        language: code.language,
        judge_mode: code.judgeMode,
        verdict: result.value.verdict,
        passed_cases: result.value.submission.passed_cases,
        total_cases: result.value.submission.total_cases,
      },
      include_roadmap: true,
      weeks: 6,
    })
    if (!pipeline?.task_id) {
      ElMessage.error('任务提交失败')
      return
    }
    taskProgress.value = pipeline

    abortTask?.()
    abortTask = streamTask(pipeline.task_id, {
      onProgress: (d) => {
        if (taskProgress.value) {
          taskProgress.value.progress = d.progress
          taskProgress.value.stage = d.stage
        }
      },
      onSummary: (s) => {
        const agents = (s.agents || {}) as Record<string, Record<string, unknown>>
        pipelineAgents.value = Object.entries(agents).map(([name, v]) => ({
          name,
          status: String(v.status ?? ''),
          degraded: Boolean(v.degraded),
          confidence: Number(v.confidence || 0),
          latency_ms: Number(v.latency_ms || 0),
        }))
      },
      onDone: () => {
        ElMessage.success('Agent 链路执行完成')
      },
      onError: (m) => ElMessage.error(`任务失败：${m}`),
    })
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function cancelPipeline() {
  const id = taskProgress.value?.task_id
  if (!id) return
  try {
    await ElMessageBox.confirm('中断任务后不再产生模型调用，确认？', '中断任务', { type: 'warning' })
  } catch {
    return
  }
  const res = await agentApi.cancelTask(id)
  ElMessage.info(res?.ok ? '已请求中断' : '任务已结束，无需中断')
}

/** 单 Agent token 级流式演示 */
function runStream() {
  if (streaming.value) {
    abortStream?.()
    streaming.value = false
    return
  }
  streaming.value = true
  streamText.value = ''
  streamFields.value = []
  streamAgentName.value = 'solution'

  abortStream = streamAgent(
    {
      agent: 'solution',
      payload: {
        title: problem.value?.title || '',
        statement: problem.value?.statement || '',
        tags: problem.value?.tags || [],
        language: code.language,
      },
    },
    {
      onToken: (t) => {
        streamText.value += t
      },
      onField: (key, value) => {
        const idx = streamFields.value.findIndex((f) => f.key === key)
        if (idx >= 0) streamFields.value[idx].value = value
        else streamFields.value.push({ key, value })
      },
      onDone: () => {
        streaming.value = false
        ElMessage.success('流式生成完成（观察字段是逐块出现的，不是最后一次性给出）')
      },
      onError: (m) => {
        streaming.value = false
        ElMessage.error(`流式失败：${m}`)
      },
      onClose: () => {
        streaming.value = false
      },
    },
  )
}

function viewSubmission(id: string) {
  void router.push({ path: '/submissions', query: { id } })
}

watch(problemId, () => {
  void loadProblem()
})

/** 举一反三：同型题检索（与题目一起加载；检索层可解释） */
const similarLoading = ref(false)
const similar = ref<{ items: SimilarProblem[]; retrieval: { rebuilt: boolean; rows: number; candidates: number } }>({
  items: [],
  retrieval: { rebuilt: false, rows: 0, candidates: 0 },
})

async function loadSimilar() {
  similarLoading.value = true
  try {
    const res = await problemApi.similar(problemId.value)
    similar.value = res
  } catch {
    /* 检索失败不阻塞做题主流程 */
  } finally {
    similarLoading.value = false
  }
}

onMounted(() => {
  void loadProblem()
  void loadSimilar()
})

onBeforeUnmount(() => {
  abortStream?.()
  abortTask?.()
})
</script>

<template>
  <div v-loading="loading">
    <el-page-header v-if="problem" style="margin-bottom: 14px" @back="router.back()">
      <template #content>
        <span style="font-weight: 600; font-size: 16px">{{ problem.title }}</span>
        <el-tag size="small" :type="difficultyType(problem.difficulty)" effect="plain" style="margin-left: 8px">
          {{ problem.difficulty }}
        </el-tag>
      </template>
    </el-page-header>

    <el-row :gutter="16">
      <!-- 左：题面 -->
      <el-col :xs="24" :md="11">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span>题目描述</span>
              <div>
                <el-tag
                  v-for="t in problem?.tags || []"
                  :key="t"
                  size="small"
                  effect="plain"
                  style="margin-left: 4px"
                >
                  {{ t }}
                </el-tag>
              </div>
            </div>
          </template>

          <div class="prose">{{ problem?.statement || '（无题面）' }}</div>

          <template v-if="problem?.input_desc">
            <el-divider content-position="left">输入格式</el-divider>
            <div class="prose">{{ problem.input_desc }}</div>
          </template>

          <template v-if="problem?.output_desc">
            <el-divider content-position="left">输出格式</el-divider>
            <div class="prose">{{ problem.output_desc }}</div>
          </template>

          <template v-if="problem?.samples?.length">
            <el-divider content-position="left">样例</el-divider>
            <div v-for="(s, i) in problem.samples" :key="i" class="sample">
              <div class="sample-col">
                <div class="stat-label">输入</div>
                <pre class="code-block">{{ s.input || s.input_data }}</pre>
              </div>
              <div class="sample-col">
                <div class="stat-label">输出</div>
                <pre class="code-block">{{ s.output || s.expected_output }}</pre>
              </div>
            </div>
          </template>

          <template v-if="problem?.constraints">
            <el-divider content-position="left">数据范围</el-divider>
            <div class="prose">{{ problem.constraints }}</div>
          </template>

          <el-divider content-position="left">
            测试用例（{{ testcases.length }} 个，
            {{ testcases.filter((c) => c.hidden).length }} 个隐藏）
          </el-divider>
          <div v-if="testcases.length" class="case-list">
            <el-tag
              v-for="c in testcases"
              :key="c.id"
              size="small"
              :type="c.hidden ? 'warning' : 'success'"
              effect="plain"
            >
              #{{ c.order_no }} {{ c.hidden ? '隐藏' : '可见' }}
            </el-tag>
          </div>
          <div v-else class="stat-label">
            ⚠️ 本题未配置测试用例，提交将使用"调用方判定"模式（不做真实执行）
          </div>

          <el-divider content-position="left">判题后端</el-divider>
          <div class="stat-label" style="line-height: 1.9">
            后端：<b>{{ judgeInfo?.backend }}</b> ·
            隔离：<b :style="{ color: judgeInfo?.isolation === 'weak' ? '#E6A23C' : '#67C23A' }">{{ judgeInfo?.isolation }}</b><br />
            语言：{{ (judgeInfo?.languages || []).join(' / ') || '无' }}<br />
            限制：{{ problem?.time_limit_ms }}ms / {{ problem?.memory_limit_mb }}MB
          </div>
        </el-card>
      </el-col>

      <!-- 右：代码与结果 -->
      <el-col :xs="24" :md="13">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
              <el-select v-model="code.language" size="small" style="width: 120px" @change="switchLanguage">
                <el-option
                  v-for="l in judgeInfo?.languages || ['python']"
                  :key="l"
                  :label="l"
                  :value="l"
                />
              </el-select>
              <el-select v-model="code.judgeMode" size="small" style="width: 150px">
                <el-option label="自动判定" value="auto" />
                <el-option label="沙箱判题" value="sandbox" />
                <el-option label="调用方判定" value="caller" />
              </el-select>
              <el-checkbox v-model="code.runAgents" size="small">提交后跑 Agent 归因</el-checkbox>
              <div class="flex-spacer" />
              <el-button size="small" @click="resetCode">重置模板</el-button>
            </div>
          </template>

          <el-input
            v-model="code.source"
            type="textarea"
            :rows="16"
            class="mono"
            placeholder="在此编写代码"
            spellcheck="false"
          />

          <div class="toolbar" style="margin-top: 12px; margin-bottom: 0">
            <el-button type="primary" :loading="submitting" @click="submit">
              <el-icon><VideoPlay /></el-icon> 提交判题
            </el-button>
            <el-button :loading="hintLoading" @click="loadHint">
              <el-icon><QuestionFilled /></el-icon> 我要提示
            </el-button>
            <el-button :disabled="!result" @click="runPipeline">
              <el-icon><MagicStick /></el-icon> 跑完整 Agent 链路
            </el-button>
            <el-button :disabled="!taskProgress" @click="cancelPipeline">中断任务</el-button>
            <el-button @click="runStream">
              <el-icon><Promotion /></el-icon>
              {{ streaming ? '停止流式' : '流式生成题解' }}
            </el-button>
          </div>
        </el-card>

        <!-- 渐进式提示（强度由掌握度自动决定） -->
        <el-card v-if="hint" class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>解题提示</span>
              <el-tag v-if="hintMeta" :type="hintMeta.type" size="small" effect="plain">
                {{ hintMeta.label }}
              </el-tag>
              <el-tag v-if="hint.degraded" type="info" size="small" effect="plain">规则引擎</el-tag>
              <div class="flex-spacer" />
              <el-button link size="small" @click="hint = null">收起</el-button>
            </div>
          </template>

          <el-alert type="info" :closable="false" style="margin-bottom: 12px">
            <template #default>
              <div style="font-size: 13px">{{ hint.strength_reason }}</div>
              <div v-if="hint.memory_tokens" style="font-size: 12px; color: #909399; margin-top: 4px">
                已注入学习记忆（约 {{ hint.memory_tokens }} token 的聚合结论，非完整历史）
              </div>
            </template>
          </el-alert>

          <ol class="hint-list">
            <li v-for="(h, i) in hint.hints" :key="i">{{ h }}</li>
          </ol>

          <el-descriptions :column="1" border size="small" style="margin-top: 12px">
            <el-descriptions-item label="突破口">{{ hint.key_insight }}</el-descriptions-item>
            <el-descriptions-item label="下一步">{{ hint.next_step }}</el-descriptions-item>
          </el-descriptions>

          <div style="margin-top: 12px; text-align: right">
            <el-button link size="small" @click="router.push(`/chat/${problemId}`)">
              还是不懂？进入对话式辅导 →
            </el-button>
          </div>
        </el-card>

        <!-- 举一反三：同型题检索（FTS5 + 标签/难度混合排序） -->
        <el-card v-if="similar.items.length" class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>举一反三 · 同型题</span>
              <el-tag size="small" effect="plain" type="info">
                {{ similar.retrieval.candidates }} 个候选 · {{ similar.retrieval.rebuilt ? '索引已重建' : '索引复用' }}
              </el-tag>
              <div class="flex-spacer" />
              <el-button link size="small" :loading="similarLoading" @click="loadSimilar">刷新</el-button>
            </div>
          </template>
          <div class="similar-list">
            <div
              v-for="item in similar.items"
              :key="item.problem_id"
              class="similar-item"
              @click="router.push(`/problems/${item.problem_id}`)"
            >
              <div class="similar-title">
                <el-tag size="small" :type="difficultyType(item.difficulty)" effect="plain">
                  {{ item.difficulty }}
                </el-tag>
                <span>{{ item.title }}</span>
              </div>
              <div class="similar-reason">{{ item.reason }}</div>
            </div>
          </div>
        </el-card>

        <!-- 判题结果 -->
        <el-card v-if="result" class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 10px">
              <span>判题结果</span>
              <el-tag :type="verdictType(result.verdict)" effect="dark">
                {{ result.verdict }} · {{ verdictText(result.verdict) }}
              </el-tag>
              <el-tag size="small" effect="plain">{{ result.judge_mode }}</el-tag>
              <el-tag
                size="small"
                :type="result.submission.judged_by === 'sandbox' ? 'success' : 'info'"
                effect="plain"
              >
                {{ result.submission.judged_by === 'sandbox' ? '真实执行' : '调用方判定' }}
              </el-tag>
              <div class="flex-spacer" />
              <span class="stat-label">
                {{ result.submission.passed_cases }}/{{ result.submission.total_cases }} 用例 ·
                {{ result.submission.runtime_ms }}ms
              </span>
            </div>
          </template>

          <el-progress :percentage="passedRatio" :status="result.verdict === 'AC' ? 'success' : 'exception'" />

          <el-table v-if="detailCases.length" :data="detailCases" size="small" style="margin-top: 12px">
            <el-table-column prop="case_id" label="用例" width="130" />
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="verdictType(row.verdict)" effect="plain">
                  {{ row.verdict }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="time_ms" label="耗时(ms)" width="90" />
            <el-table-column prop="message" label="说明" min-width="160" />
          </el-table>

          <el-alert
            v-if="result.judge_detail?.compile_output"
            type="error"
            :closable="false"
            style="margin-top: 12px"
          >
            <template #title>编译输出</template>
            <pre class="code-block" style="margin-top: 6px">{{ result.judge_detail.compile_output }}</pre>
          </el-alert>

          <el-alert
            v-if="result.judge_detail?.message && !detailCases.length"
            type="info"
            :closable="false"
            style="margin-top: 12px"
          >
            {{ result.judge_detail.message }}
          </el-alert>

          <!-- Agent 归因 -->
          <template v-if="result.judge">
            <el-divider content-position="left">
              Agent 判错归因
              <el-tag v-if="result.judge.degraded" size="small" type="warning" effect="plain" style="margin-left: 6px">
                规则引擎兜底
              </el-tag>
            </el-divider>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="根因分类">
                {{ result.judge.root_cause || '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="置信度">
                {{ (result.judge.confidence * 100).toFixed(0) }}%
              </el-descriptions-item>
              <el-descriptions-item label="问题说明" :span="2">
                {{ result.judge.explanation }}
              </el-descriptions-item>
              <el-descriptions-item label="修复建议" :span="2">
                {{ result.judge.fix_suggestion }}
              </el-descriptions-item>
            </el-descriptions>
          </template>
        </el-card>

        <!-- 后台任务进度 -->
        <el-card v-if="taskProgress" class="page-card" shadow="never">
          <template #header>Agent 链路进度（后台任务 + SSE）</template>
          <el-progress :percentage="Math.round(taskProgress.progress * 100)" />
          <div class="stat-label" style="margin-top: 6px">
            阶段：{{ taskProgress.stage }} · 状态：{{ taskProgress.status }} · task_id：
            <span class="mono">{{ taskProgress.task_id }}</span>
          </div>
          <el-table v-if="pipelineAgents.length" :data="pipelineAgents" size="small" style="margin-top: 10px">
            <el-table-column prop="name" label="Agent" width="120" />
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="row.degraded ? 'warning' : 'success'" effect="plain">
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="置信度" width="100">
              <template #default="{ row }">{{ (row.confidence * 100).toFixed(0) }}%</template>
            </el-table-column>
            <el-table-column prop="latency_ms" label="耗时(ms)" />
          </el-table>
        </el-card>

        <!-- 流式面板 -->
        <el-card v-if="streaming || streamText" class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 8px">
              <span>token 级流式（{{ streamAgentName }}）</span>
              <el-tag size="small" effect="plain">{{ streamFields.length }} 个字段已闭合</el-tag>
            </div>
          </template>
          <div class="stream-box">
            {{ streamText }}<span v-if="streaming" class="stream-cursor" />
          </div>
          <div v-if="streamFields.length" style="margin-top: 10px">
            <div class="stat-label" style="margin-bottom: 6px">
              增量字段（闭合即推送，不必等整个 JSON 生成完）
            </div>
            <el-tag v-for="f in streamFields" :key="f.key" size="small" effect="plain" style="margin: 0 4px 4px 0">
              {{ f.key }}
            </el-tag>
          </div>
        </el-card>

        <!-- 历史提交 -->
        <el-card class="page-card" shadow="never">
          <template #header>本题历史提交</template>
          <el-table :data="history" size="small">
            <el-table-column label="时间" width="90">
              <template #default="{ row }">{{ fmtRelative(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="verdictType(row.verdict)" effect="plain">
                  {{ row.verdict }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="用例" width="90">
              <template #default="{ row }">{{ row.passed_cases }}/{{ row.total_cases }}</template>
            </el-table-column>
            <el-table-column prop="language" label="语言" width="90" />
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ row.runtime_ms }}ms</template>
            </el-table-column>
            <el-table-column label="" min-width="80">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="viewSubmission(row.id)">
                  详情
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <div class="empty-hint">还没有提交记录</div>
            </template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.sample {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
}

.sample-col {
  flex: 1;
  min-width: 0;
}

.case-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* 提示列表：按强度递进展示，行距放宽便于逐条阅读 */
.hint-list {
  margin: 0;
  padding-left: 22px;
  line-height: 1.9;
}

.hint-list li {
  margin-bottom: 6px;
}
</style>
<style scoped>
.similar-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.similar-item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.similar-item:hover {
  border-color: var(--el-color-primary-light-5);
}
.similar-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}
.similar-reason {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
