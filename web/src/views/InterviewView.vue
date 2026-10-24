<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { interviewApi, problemApi, type InterviewReportOut, type InterviewSessionOut } from '@/api'
import { verdictText, verdictType } from '@/utils/format'

type Phase = 'lobby' | 'live' | 'report'
const phase = ref<Phase>('lobby')

// --- 开局面板 ---
const startProblemId = ref('')
const startMinutes = ref(30)
const starting = ref(false)
const sessions = ref<InterviewSessionOut[]>([])
const problemOptions = ref<Array<{ id: string; title: string; difficulty: string }>>([])

// --- 面试间 ---
const current = ref<InterviewSessionOut | null>(null)
const messages = ref<Array<{ role: string; content: string }>>([])
const input = ref('')
const sending = ref(false)
const code = ref('')
const lang = ref('python')
const submitting = ref(false)
const lastVerdict = ref<{ verdict: string; passed: number; total: number } | null>(null)
const elapsed = ref(0)
let timer: number | null = null

// --- 报告 ---
const report = ref<InterviewReportOut | null>(null)
const SCORE_META: Record<string, string> = {
  correctness: '正确性',
  code_quality: '代码质量',
  hint_dependency: '提示依赖',
  time_efficiency: '时间效率',
  communication: '沟通质量',
  total: '总分',
}

const remainingText = computed(() => {
  if (!current.value) return ''
  const limitS = current.value.time_limit_minutes * 60
  const remain = Math.max(0, limitS - elapsed.value)
  const m = Math.floor(remain / 60)
  const s = Math.floor(remain % 60)
  return `${m}:${String(s).padStart(2, '0')}`
})

async function loadLobby() {
  const [list, problems] = await Promise.all([interviewApi.list(), problemApi.list({ page: 1, size: 50 })])
  sessions.value = list.items
  problemOptions.value = problems.items.map((p) => ({ id: p.id, title: p.title, difficulty: p.difficulty }))
}

async function start() {
  if (!startProblemId.value) {
    ElMessage.warning('先选择题库里的题目')
    return
  }
  starting.value = true
  try {
    current.value = await interviewApi.start(startProblemId.value, startMinutes.value)
    messages.value = []
    lastVerdict.value = null
    code.value = ''
    elapsed.value = 0
    phase.value = 'live'
    startTimer()
  } finally {
    starting.value = false
  }
}

async function resume(s: InterviewSessionOut) {
  const detail = await interviewApi.detail(s.id)
  current.value = s
  messages.value = detail.messages
  phase.value = 'live'
  elapsed.value = 0
  startTimer()
}

function startTimer() {
  stopTimer()
  timer = window.setInterval(() => {
    elapsed.value += 1
  }, 1000)
}

function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

async function send(askHint = false) {
  const content = askHint ? '请给我一点提示' : input.value.trim()
  if (!content || sending.value || !current.value) return
  if (!askHint) input.value = ''
  messages.value.push({ role: 'user', content })
  sending.value = true
  try {
    const out = await interviewApi.message(current.value.id, content, askHint)
    messages.value.push({ role: 'assistant', content: out.reply })
    elapsed.value = out.elapsed_seconds
  } finally {
    sending.value = false
  }
}

async function submitCode() {
  if (!current.value || submitting.value) return
  submitting.value = true
  try {
    const out = await interviewApi.submit(current.value.id, lang.value, code.value)
    lastVerdict.value = { verdict: out.verdict, passed: out.passed, total: out.total }
    messages.value.push({
      role: 'assistant',
      content: `[已提交] 判题 ${out.verdict}（${out.passed}/${out.total}）。请继续优化，或告诉我你的下一步思路。`,
    })
  } finally {
    submitting.value = false
  }
}

async function finish(showReport = true) {
  if (!current.value) return
  stopTimer()
  try {
    const out = await interviewApi.finish(current.value.id)
    if (showReport && out.scores) {
      report.value = {
        verdict: (lastVerdict.value?.verdict || '') as string,
        scores: out.scores,
        narrative: out.narrative || '',
        degraded: !!out.degraded,
        created_at: new Date().toISOString(),
      }
      phase.value = 'report'
    } else {
      phase.value = 'lobby'
      void loadLobby()
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function openReport(s: InterviewSessionOut) {
  const out = await interviewApi.report(s.id)
  if (!out.report) {
    ElMessage.info('该场次还没有报告（可能未结束）')
    return
  }
  report.value = out.report
  current.value = s
  phase.value = 'report'
}

async function maybeTimeUp() {
  if (phase.value !== 'live' || !current.value) return
  if (current.value.time_limit_minutes > 0 && elapsed.value >= current.value.time_limit_minutes * 60) {
    stopTimer()
    ElMessageBox.confirm('已到时限，结束本场面试并生成报告？', '时间到', { type: 'warning' })
      .then(() => finish(true))
      .catch(() => undefined)
  }
}

onMounted(() => {
  void loadLobby()
})
onBeforeUnmount(stopTimer)
</script>

<script lang="ts">
export default { name: 'InterviewView' }
</script>

<template>
  <div>
    <!-- ==================== 开局 / 历史 ==================== -->
    <template v-if="phase === 'lobby'">
      <el-card class="page-card" shadow="never" style="margin-bottom: 14px">
        <template #header><span>开始一场模拟面试</span></template>
        <el-form label-width="90px" style="max-width: 560px">
          <el-form-item label="题目">
            <el-select v-model="startProblemId" filterable placeholder="选择题目" style="width: 100%">
              <el-option
                v-for="p in problemOptions"
                :key="p.id"
                :value="p.id"
                :label="`${p.title}（${p.difficulty}）`"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="时限（分）">
            <el-input-number v-model="startMinutes" :min="5" :max="90" :step="5" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="starting" @click="start">开始面试</el-button>
          </el-form-item>
        </el-form>
        <el-alert type="info" :closable="false" title="面试规则" description="考官只考察不教学：默认不给提示；主动索要会计入「提示依赖」评分。结束后生成五维复盘报告。" />
      </el-card>

      <el-card class="page-card" shadow="never">
        <template #header><span>历史场次</span></template>
        <el-table :data="sessions" size="small">
          <el-table-column prop="title" label="题目" min-width="200" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="hint_requests" label="提示次数" width="90" />
          <el-table-column prop="message_count" label="消息数" width="90" />
          <el-table-column label="操作" width="160">
            <template #default="{ row }">
              <el-button v-if="row.status === 'ongoing'" link type="primary" @click="resume(row)">继续</el-button>
              <el-button link @click="openReport(row)">查看报告</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!sessions.length" description="还没有面试记录" :image-size="60" />
      </el-card>
    </template>

    <!-- ==================== 面试间 ==================== -->
    <template v-else-if="phase === 'live' && current">
      <div style="display: flex; gap: 14px; align-items: center; margin-bottom: 10px">
        <span style="font-weight: 600">{{ current.title }}</span>
        <el-tag :type="elapsed >= current.time_limit_minutes * 60 ? 'danger' : 'info'" effect="dark" size="large">
          {{ remainingText }}
        </el-tag>
        <el-tag size="small" effect="plain">提示 {{ current.hint_requests }} 次</el-tag>
        <div class="flex-spacer" />
        <el-button @click="maybeTimeUp(); finish(true)">结束并出报告</el-button>
      </div>

      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px">
        <el-card class="page-card" shadow="never" style="display: flex; flex-direction: column">
          <template #header><span>面试对话</span></template>
          <div style="max-height: 46vh; overflow: auto; margin-bottom: 10px">
            <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
              <div class="bubble" :class="m.role">{{ m.content }}</div>
            </div>
            <el-empty v-if="!messages.length" description="考官在等你先讲思路" :image-size="60" />
          </div>
          <div style="display: flex; gap: 8px; align-items: flex-end">
            <el-input
              v-model="input"
              type="textarea"
              :rows="2"
              placeholder="向考官陈述思路（Enter 发送）"
              @keydown.enter.exact.prevent="send($event)"
            />
            <el-button type="primary" :loading="sending" @click="send(false)">发送</el-button>
            <el-button :loading="sending" @click="send(true)">要提示</el-button>
          </div>
        </el-card>

        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 8px">
              <span>代码</span>
              <el-select v-model="lang" size="small" style="width: 110px">
                <el-option value="python" label="python" />
                <el-option value="cpp" label="cpp" />
                <el-option value="java" label="java" />
              </el-select>
              <div class="flex-spacer" />
              <el-tag v-if="lastVerdict" :type="verdictType(lastVerdict.verdict)" size="small" effect="dark">
                {{ lastVerdict.verdict }} {{ lastVerdict.passed }}/{{ lastVerdict.total }}
              </el-tag>
            </div>
          </template>
          <el-input
            v-model="code"
            type="textarea"
            :rows="16"
            class="mono"
            placeholder="在此编写代码，随时提交实测"
            spellcheck="false"
          />
          <el-button type="primary" style="margin-top: 10px" :loading="submitting" @click="submitCode">
            提交判题
          </el-button>
        </el-card>
      </div>
    </template>

    <!-- ==================== 报告 ==================== -->
    <template v-else-if="phase === 'report' && report">
      <el-card class="page-card" shadow="never" style="margin-bottom: 14px">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px">
            <span>模拟面试复盘报告</span>
            <el-tag v-if="report.verdict" :type="verdictType(report.verdict)" effect="dark">
              {{ report.verdict }} · {{ verdictText(report.verdict) }}
            </el-tag>
            <el-tag v-if="report.degraded" size="small" effect="plain" type="info">模板叙事</el-tag>
            <div class="flex-spacer" />
            <el-button @click="phase = 'lobby'">返回</el-button>
          </div>
        </template>
        <div style="display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 14px">
          <el-card v-for="(label, key) in SCORE_META" :key="key" shadow="never" style="width: 150px; text-align: center">
            <div style="font-size: 26px; font-weight: 700">{{ report.scores[key] ?? '-' }}</div>
            <div style="color: #909399; font-size: 12px">{{ label }}</div>
          </el-card>
        </div>
        <div style="white-space: pre-wrap; line-height: 1.8">{{ report.narrative }}</div>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  margin-bottom: 8px;
}
.msg-row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 80%;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}
.bubble.user {
  background: var(--el-color-primary-light-9);
}
.bubble.assistant {
  background: var(--el-fill-color-lighter);
}
</style>
