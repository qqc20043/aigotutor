<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { chatApi, type ChatMessageOut, type ChatSessionOut } from '@/api'
import { getToken } from '@/api/http'
import { fmtRelative } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const problemId = computed(() => String(route.params.problemId || ''))
const sessions = ref<ChatSessionOut[]>([])
const current = ref<ChatSessionOut | null>(null)
const messages = ref<ChatMessageOut[]>([])
const input = ref('')
const sending = ref(false)
const streamText = ref('')
const listRef = ref<HTMLElement | null>(null)
let abort: (() => void) | null = null

const STRENGTH_META: Record<string, { label: string; type: 'info' | 'warning' | 'danger' }> = {
  direction: { label: '方向提示', type: 'info' },
  key_observation: { label: '关键观察', type: 'warning' },
  pseudocode: { label: '步骤骨架', type: 'danger' },
}

const strengthMeta = computed(() =>
  current.value?.strength ? STRENGTH_META[current.value.strength] || STRENGTH_META.key_observation : null,
)

function scrollBottom() {
  void nextTick(() => {
    if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
  })
}

async function loadSessions() {
  const res = await chatApi.sessions()
  sessions.value = res.items
}

async function openSession(s: ChatSessionOut) {
  abort?.()
  streamText.value = ''
  current.value = s
  const res = await chatApi.detail(s.id)
  messages.value = res.messages
  scrollBottom()
}

/** 进入绑题会话：复用该题最近会话，没有则新建 */
async function openOrCreateForProblem() {
  const res = await chatApi.sessions(50)
  sessions.value = res.items
  const existing = res.items.find((s) => s.problem_id === problemId.value)
  if (existing) {
    await openSession(existing)
    return
  }
  await createSession()
}

async function createSession() {
  const s = await chatApi.createSession(problemId.value)
  current.value = s
  messages.value = []
  await loadSessions()
}

/** 发送一条消息：POST + SSE 流式解析（open -> token* -> done） */
async function send() {
  const content = input.value.trim()
  if (!content || sending.value || !current.value) return
  input.value = ''
  sending.value = true
  streamText.value = ''
  messages.value.push({
    id: `local-${Date.now()}`,
    role: 'user',
    content,
    strength: '',
    degraded: false,
    created_at: new Date().toISOString(),
  })
  scrollBottom()

  const controller = new AbortController()
  abort = () => controller.abort()
  try {
    const resp = await fetch(`/api/chat/sessions/${current.value.id}/messages?stream=true`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      body: JSON.stringify({ content }),
      signal: controller.signal,
    })
    if (!resp.ok || !resp.body) {
      throw new Error(`HTTP ${resp.status}`)
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let sep = buffer.indexOf('\n\n')
      while (sep >= 0) {
        const frame = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        handleFrame(frame)
        sep = buffer.indexOf('\n\n')
      }
    }
  } catch (e) {
    if ((e as Error).name !== 'AbortError') {
      ElMessage.error(`发送失败：${(e as Error).message}`)
      messages.value = messages.value.filter((m) => !m.id.startsWith('local-'))
      input.value = content
    }
  } finally {
    sending.value = false
    abort = null
    // done 事件已把最终消息入列；清理流式缓冲
    streamText.value = ''
  }
}

function handleFrame(frame: string) {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return
  let payload: Record<string, any>
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  if (event === 'token') {
    streamText.value += payload.text || ''
    scrollBottom()
  } else if (event === 'done') {
    messages.value.push({
      id: `done-${Date.now()}`,
      role: 'assistant',
      content: payload.reply || '',
      strength: payload.strength || '',
      degraded: !!payload.degraded,
      created_at: new Date().toISOString(),
    })
    if (current.value && payload.strength) current.value.strength = payload.strength
    streamText.value = ''
    scrollBottom()
    void loadSessions()
  }
}

onMounted(() => {
  if (problemId.value) void openOrCreateForProblem()
  else void loadSessions()
})

onBeforeUnmount(() => abort?.())
</script>

<template>
  <div class="chat-page">
    <!-- 会话列表 -->
    <div class="session-panel page-card">
      <div class="session-head">
        <span style="font-weight: 600">辅导会话</span>
        <el-button size="small" type="primary" plain @click="createSession">新建</el-button>
      </div>
      <div
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ active: current?.id === s.id }"
        @click="openSession(s)"
      >
        <div class="session-title">{{ s.title }}</div>
        <div class="session-meta">
          {{ s.message_count }} 条 · {{ fmtRelative(s.updated_at) }}
        </div>
      </div>
      <el-empty v-if="!sessions.length" description="还没有会话" :image-size="60" />
    </div>

    <!-- 对话区 -->
    <div class="chat-main page-card">
      <div class="chat-head">
        <span style="font-weight: 600">对话式辅导</span>
        <el-tag v-if="strengthMeta" :type="strengthMeta.type" size="small" effect="plain">
          当前强度：{{ strengthMeta.label }}
        </el-tag>
        <el-tag v-if="current?.problem_id" size="small" effect="plain" type="info">已绑定题目</el-tag>
        <div class="flex-spacer" />
        <el-button
          v-if="current?.problem_id"
          link
          size="small"
          @click="router.push(`/problems/${current.problem_id}`)"
        >
          回到题目 →
        </el-button>
      </div>

      <div ref="listRef" class="msg-list">
        <el-empty
          v-if="!messages.length && !streamText"
          description="问教练任何问题：&#10;「这题完全没思路」「我想到了 X，对吗」「再点拨一下」"
        />
        <div v-for="m in messages" :key="m.id" class="msg-row" :class="m.role">
          <div class="bubble" :class="m.role">
            <div v-if="m.role === 'assistant'" class="bubble-meta">
              <el-tag
                v-if="m.strength && STRENGTH_META[m.strength]"
                :type="STRENGTH_META[m.strength].type"
                size="small"
                effect="plain"
              >
                {{ STRENGTH_META[m.strength].label }}
              </el-tag>
              <el-tag v-if="m.degraded" size="small" effect="plain" type="info">规则引擎</el-tag>
            </div>
            <div class="bubble-text">{{ m.content }}</div>
          </div>
        </div>
        <div v-if="streamText" class="msg-row assistant">
          <div class="bubble assistant">
            <div class="bubble-text">{{ streamText }}<span class="cursor">▍</span></div>
          </div>
        </div>
      </div>

      <div class="chat-input">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          placeholder="向教练提问（Enter 发送，Shift+Enter 换行）"
          :disabled="sending"
          @keydown.enter.exact.prevent="send"
        />
        <el-button type="primary" :loading="sending" @click="send">发送</el-button>
      </div>
    </div>
  </div>
</template>

<script lang="ts">
// 模板里用到的常量（非 setup 响应式）
export default { name: 'ChatView' }
</script>

<style scoped>
.chat-page {
  display: flex;
  gap: 14px;
  height: calc(100vh - 130px);
}
.session-panel {
  width: 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  overflow: auto;
}
.session-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.session-item {
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
}
.session-item:hover {
  background: var(--el-fill-color-light);
}
.session-item.active {
  background: var(--el-color-primary-light-9);
}
.session-title {
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-meta {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.msg-list {
  flex: 1;
  overflow: auto;
  padding: 6px 4px;
}
.msg-row {
  display: flex;
  margin-bottom: 10px;
}
.msg-row.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 72%;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 14px;
  line-height: 1.6;
}
.bubble.user {
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
}
.bubble.assistant {
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
}
.bubble-meta {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}
.chat-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-top: 10px;
}
.cursor {
  animation: blink 1s infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
</style>
