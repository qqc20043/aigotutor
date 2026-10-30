<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adminApi } from '@/api'
import { errorText } from '@/api/http'
import { useUserStore } from '@/stores/user'
import { fmtDate } from '@/utils/format'

const user = useUserStore()

const loading = ref(false)
const overview = ref<Record<string, any> | null>(null)
const users = ref<Array<Record<string, any>>>([])
const userTotal = ref(0)
const health = ref<Record<string, any> | null>(null)
const cleaning = ref(false)

const query = reactive({ page: 1, size: 20, keyword: '' })
const days = ref(7)

async function load() {
  loading.value = true
  try {
    const [o, u, h] = await Promise.allSettled([
      adminApi.overview(days.value),
      adminApi.users({ page: query.page, size: query.size, keyword: query.keyword }),
      adminApi.problemsHealth(50),
    ])
    if (o.status === 'fulfilled') overview.value = o.value
    if (u.status === 'fulfilled') {
      users.value = u.value.items
      userTotal.value = u.value.total
    }
    if (h.status === 'fulfilled') health.value = h.value
  } finally {
    loading.value = false
  }
}

async function toggleActive(row: Record<string, any>) {
  const next = !row.is_active
  try {
    await ElMessageBox.confirm(
      next ? `确认启用用户 ${row.username}？` : `停用 ${row.username} 会立即踢其下线，确认？`,
      '确认操作',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    const res = await adminApi.patchUser(row.id, { is_active: next })
    ElMessage.success(res.message || '已更新')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function toggleRole(row: Record<string, any>) {
  const next = row.role === 'admin' ? 'user' : 'admin'
  try {
    await ElMessageBox.confirm(`将 ${row.username} 的角色改为 ${next}？`, '确认操作', {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await adminApi.patchUser(row.id, { role: next })
    ElMessage.success('角色已更新')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function removeProblem(row: Record<string, any>) {
  try {
    await ElMessageBox.confirm(
      `删除题目「${row.title}」会连带删除其提交记录，不可恢复。确认？`,
      '危险操作',
      { type: 'error', confirmButtonText: '确认删除' },
    )
  } catch {
    return
  }
  try {
    await adminApi.deleteProblem(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function doCleanup() {
  cleaning.value = true
  try {
    const res = await adminApi.cleanup()
    ElMessage.success(
      `清理完成：孤儿提交 ${res.orphan_submissions} 条，过期通知 ${res.old_notifications} 条`,
    )
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    cleaning.value = false
  }
}

// ---------------- 题目管理（对标 Hydro 内容工作台）----------------
const problems = ref<Array<Record<string, any>>>([])
const problemsTotal = ref(0)
const problemsLoading = ref(false)
const pQuery = reactive({ page: 1, size: 15, keyword: '' })
const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive({
  title: '',
  statement: '',
  input_desc: '',
  output_desc: '',
  constraints: '',
  tags: '',
  difficulty: '入门',
  time_limit_ms: 1000,
  memory_limit_mb: 256,
  casesText: '输入1\n输出1\n\n输入2\n输出2',
})

async function loadProblems() {
  problemsLoading.value = true
  try {
    const res = await adminApi.adminProblems({ page: pQuery.page, size: pQuery.size, keyword: pQuery.keyword || undefined })
    problems.value = res.items
    problemsTotal.value = res.total
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    problemsLoading.value = false
  }
}

/** 用例文本协议：空行分隔用例，"输入 x" / "输出 x" 行交替 */
function parseCases(text: string): Array<{ input: string; output: string; hidden: string }> {
  const out: Array<{ input: string; output: string; hidden: string }> = []
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.trim().split('\n')
    const inLine = lines.find((l) => l.startsWith('输入'))
    const outLine = lines.find((l) => l.startsWith('输出'))
    if (!inLine || !outLine) continue
    out.push({
      input: inLine.replace(/^输入\s*/, '') + '\n',
      output: outLine.replace(/^输出\s*/, '') + '\n',
      hidden: 'true',
    })
  }
  return out
}

async function createProblem() {
  if (!createForm.title.trim() || !createForm.statement.trim()) {
    ElMessage.warning('标题与题面必填')
    return
  }
  creating.value = true
  try {
    const cases = parseCases(createForm.casesText)
    const res = await adminApi.adminCreateProblem({
      title: createForm.title,
      statement: createForm.statement,
      input_desc: createForm.input_desc,
      output_desc: createForm.output_desc,
      constraints: createForm.constraints,
      tags: createForm.tags.split(/[,，\s]+/).filter(Boolean),
      difficulty: createForm.difficulty,
      time_limit_ms: createForm.time_limit_ms,
      memory_limit_mb: createForm.memory_limit_mb,
      testcases: cases,
    })
    ElMessage.success(
      res.judgeable ? `已建题「${res.title}」（${res.cases_saved} 个用例，立即可判题）` : `已建题，但未配用例（无法判题）`,
    )
    createVisible.value = false
    await loadProblems()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    creating.value = false
  }
}

// ---------------- 提交管理 ----------------
const subs = ref<Array<Record<string, any>>>([])
const subsTotal = ref(0)
const subsLoading = ref(false)
const sQuery = reactive({ page: 1, size: 15, username: '', verdict: '' })

async function loadSubmissions() {
  subsLoading.value = true
  try {
    const res = await adminApi.adminSubmissions({
      page: sQuery.page,
      size: sQuery.size,
      username: sQuery.username || undefined,
      verdict: sQuery.verdict || undefined,
    })
    subs.value = res.items
    subsTotal.value = res.total
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    subsLoading.value = false
  }
}

async function rejudge(row: Record<string, any>) {
  try {
    const res = await adminApi.adminRejudge(row.id)
    ElMessage.success(`重判完成：${res.old_verdict} -> ${res.new_verdict}`)
    await loadSubmissions()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---------------- 用户详情 ----------------
const userDetail = ref<Record<string, any> | null>(null)
const detailVisible = ref(false)

async function openUserDetail(row: Record<string, any>) {
  try {
    userDetail.value = await adminApi.adminUserDetail(row.id)
    detailVisible.value = true
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function resetPassword(row: Record<string, any>) {
  try {
    await ElMessageBox.confirm(`重置 ${row.username} 的密码？旧 token 会立即失效`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    const res = await adminApi.adminResetPassword(row.id)
    await ElMessageBox.alert(
      `临时密码：${res.temp_password}`,
      `已重置 ${res.username}`,
      { confirmButtonText: '我已记录' },
    )
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

// ---------------- 公告推送 ----------------
const announceVisible = ref(false)
const announcing = ref(false)
const announceForm = reactive({ title: '', body: '', link: '', username: '' })

async function sendAnnounce() {
  if (!announceForm.title.trim()) {
    ElMessage.warning('公告标题必填')
    return
  }
  announcing.value = true
  try {
    const res = await adminApi.adminAnnounce({
      title: announceForm.title,
      body: announceForm.body,
      link: announceForm.link,
      username: announceForm.username || undefined,
    })
    ElMessage.success(res.scope === 'broadcast' ? `已广播给 ${res.sent} 位用户` : '已发送给指定用户')
    announceVisible.value = false
    announceForm.title = ''
    announceForm.body = ''
    announceForm.link = ''
    announceForm.username = ''
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    announcing.value = false
  }
}

// ---------------- 审计日志 ----------------
const auditLogs = ref<Array<Record<string, any>>>([])
const auditTotal = ref(0)
const auditLoading = ref(false)
const aQuery = reactive({ page: 1, size: 20, action: '', actor: '' })
const AUDIT_ACTIONS = [
  { v: '', l: '全部动作' },
  { v: 'problem.create', l: '新建题目' },
  { v: 'problem.update', l: '修改题目' },
  { v: 'problem.delete', l: '删除题目' },
  { v: 'problem.testcases', l: '维护用例' },
  { v: 'user.patch', l: '修改用户' },
  { v: 'user.reset_password', l: '重置密码' },
  { v: 'submission.rejudge', l: '重判提交' },
  { v: 'announce.send', l: '发送公告' },
  { v: 'users.batch', l: '批量用户' },
  { v: 'data.cleanup', l: '数据清理' },
]

async function loadAudit() {
  auditLoading.value = true
  try {
    const res = await adminApi.adminAudit({
      page: aQuery.page,
      size: aQuery.size,
      action: aQuery.action || undefined,
      actor: aQuery.actor || undefined,
    })
    auditLogs.value = res.items
    auditTotal.value = res.total
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    auditLoading.value = false
  }
}

// ---------------- 批量用户 ----------------
const selectedUsers = ref<Array<Record<string, any>>>([])
const batching = ref(false)

function onUserSelection(rows: Array<Record<string, any>>) {
  selectedUsers.value = rows
}

async function batchUsers(patch: { is_active?: boolean; role?: string; daily_goal?: number }) {
  if (!selectedUsers.value.length) {
    ElMessage.warning('请先勾选用户')
    return
  }
  batching.value = true
  try {
    const res = await adminApi.adminBatchUsers({
      user_ids: selectedUsers.value.map((u) => u.id),
      ...patch,
    })
    ElMessage.success(`已更新 ${res.updated} 人${res.skipped.length ? `，跳过 ${res.skipped.length} 人` : ''}`)
    await load()
    await loadAudit()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    batching.value = false
  }
}

// ---------------- 题目用例编辑 ----------------
const casesVisible = ref(false)
const casesLoading = ref(false)
const casesSaving = ref(false)
const casesProblem = ref<Record<string, any> | null>(null)
const casesText = ref('')

async function openCases(row: Record<string, any>) {
  casesProblem.value = row
  casesVisible.value = true
  casesLoading.value = true
  try {
    const res = await adminApi.adminTestcases(row.id)
    casesText.value = res.items
      .map((c) => `输入 ${c.input.trimEnd()}\n输出 ${c.output.trimEnd()}`)
      .join('\n\n')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    casesLoading.value = false
  }
}

const CASE_SEP = /\n\s*\n/

function parseCasesText(text: string): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = []
  for (const block of text.split(CASE_SEP)) {
    const lines = block.trim().split('\n')
    const inIdx = lines.findIndex((l) => l.startsWith('输入'))
    const outIdx = lines.findIndex((l) => l.startsWith('输出'))
    if (inIdx < 0 || outIdx < 0) continue
    const input = lines
      .slice(inIdx, outIdx)
      .map((l, i) => (i === 0 ? l.replace(/^输入\s*/, '') : l))
      .join('\n')
    const output = lines
      .slice(outIdx)
      .map((l, i) => (i === 0 ? l.replace(/^输出\s*/, '') : l))
      .join('\n')
    out.push({ input: input + '\n', output: output + '\n', hidden: 'true' })
  }
  return out
}

async function saveCases() {
  if (!casesProblem.value) return
  const cases = parseCasesText(casesText.value)
  if (!cases.length) {
    ElMessage.warning('至少需要一个「输入 / 输出」用例')
    return
  }
  casesSaving.value = true
  try {
    const res = await adminApi.adminReplaceTestcases(casesProblem.value.id, cases)
    ElMessage.success(`已保存 ${res.saved} 个用例（原 ${res.old_count} 个）`)
    casesVisible.value = false
    await loadProblems()
    await loadAudit()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    casesSaving.value = false
  }
}

onMounted(() => {
  void load()
  void loadProblems()
  void loadSubmissions()
  void loadAudit()
})
</script>

<template>
  <div v-loading="loading">
    <el-alert
      v-if="!user.isAdmin"
      type="warning"
      :closable="false"
      style="margin-bottom: 16px"
    >
      当前账号不是管理员，管理接口会返回 403。首个注册用户自动成为管理员。
    </el-alert>

    <!-- 概览 -->
    <el-row :gutter="16">
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value">{{ overview?.users?.total ?? '-' }}</div>
          <div class="stat-label">用户总数</div>
          <div class="stat-label">管理员 {{ overview?.users?.admins ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value">{{ overview?.problems?.total ?? '-' }}</div>
          <div class="stat-label">题目总数</div>
          <div class="stat-label">用例 {{ overview?.problems?.testcases ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value">{{ overview?.submissions?.total ?? '-' }}</div>
          <div class="stat-label">提交总数</div>
          <div class="stat-label">近 {{ days }} 天 {{ overview?.submissions?.recent ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #e6a23c">
            {{ ((overview?.agents?.degraded_rate ?? 0) * 100).toFixed(1) }}%
          </div>
          <div class="stat-label">Agent 降级率</div>
          <div class="stat-label">调用 {{ overview?.agents?.calls ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #67c23a">
            {{ overview?.agents?.tokens ?? 0 }}
          </div>
          <div class="stat-label">Token 消耗</div>
          <div class="stat-label">失败 {{ overview?.agents?.failed ?? 0 }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #f56c6c">
            {{ overview?.problems?.without_testcases ?? 0 }}
          </div>
          <div class="stat-label">缺用例的题</div>
          <div class="stat-label">无法真实判题</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <span class="stat-label">统计窗口</span>
        <el-select v-model="days" size="small" style="width: 120px" @change="load">
          <el-option :value="1" label="近 1 天" />
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
        </el-select>
        <div class="flex-spacer" />
        <el-button size="small" :loading="cleaning" @click="doCleanup">数据清理</el-button>
        <el-button size="small" @click="load">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </el-card>

    <!-- 用户管理 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>用户管理</span>
          <el-input
            v-model="query.keyword"
            placeholder="搜索用户名/昵称"
            size="small"
            clearable
            style="width: 200px"
            @keyup.enter="load"
          />
          <el-button size="small" @click="query.page = 1; load()">搜索</el-button>
          <div class="flex-spacer" />
          <template v-if="selectedUsers.length">
            <span class="stat-label">已选 {{ selectedUsers.length }} 人</span>
            <el-button size="small" :loading="batching" @click="batchUsers({ is_active: true })">
              批量启用
            </el-button>
            <el-button size="small" type="danger" plain :loading="batching" @click="batchUsers({ is_active: false })">
              批量停用
            </el-button>
            <el-button size="small" :loading="batching" @click="batchUsers({ role: 'admin' })">
              升为管理员
            </el-button>
            <el-button size="small" :loading="batching" @click="batchUsers({ role: 'user' })">
              降为普通用户
            </el-button>
          </template>
          <span class="stat-label">共 {{ userTotal }} 人</span>
        </div>
      </template>

      <el-table :data="users" size="small" stripe @selection-change="onUserSelection">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column prop="nickname" label="昵称" min-width="120" />
        <el-table-column label="角色" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.role === 'admin' ? 'danger' : 'info'" effect="plain">
              {{ row.role }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'info'" effect="plain">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="level" label="等级" width="80" />
        <el-table-column prop="timezone" label="时区" width="130" />
        <el-table-column label="最近登录" width="150">
          <template #default="{ row }">
            <span class="stat-label">{{ row.last_login_at ? fmtDate(row.last_login_at) : '从未' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="toggleRole(row)">
              {{ row.role === 'admin' ? '降为用户' : '设为管理员' }}
            </el-button>
            <el-button
              link
              :type="row.is_active ? 'danger' : 'success'"
              size="small"
              @click="toggleActive(row)"
            >
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
            <el-button link type="primary" size="small" @click="openUserDetail(row)">详情</el-button>
            <el-button link type="warning" size="small" @click="resetPassword(row)">重置密码</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">暂无用户</div>
        </template>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="userTotal"
          :page-sizes="[20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </el-card>

    <!-- 题库体检 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 8px">
          <span>题库体检</span>
          <el-tag size="small" type="warning" effect="plain">
            {{ health?.without_testcases ?? 0 }} 道题缺用例
          </el-tag>
        </div>
      </template>

      <el-alert v-if="health?.hint" type="info" :closable="false" style="margin-bottom: 12px">
        <span class="stat-label">{{ health.hint }}</span>
      </el-alert>

      <el-table :data="health?.items || []" size="small" max-height="360">
        <el-table-column prop="title" label="题目" min-width="200" />
        <el-table-column prop="difficulty" label="难度" width="130" />
        <el-table-column prop="source" label="来源" width="110" />
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="removeProblem(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">全部题目都配置了测试用例 👍</div>
        </template>
      </el-table>
    </el-card>

    <!-- 提交管理（全平台） -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>提交管理</span>
          <el-tag size="small" effect="plain" type="info">共 {{ subsTotal }} 条</el-tag>
          <div class="flex-spacer" />
          <el-input v-model="sQuery.username" placeholder="按用户名" size="small" style="width: 140px" clearable />
          <el-select v-model="sQuery.verdict" placeholder="判定" size="small" style="width: 110px" clearable>
            <el-option v-for="v in ['AC','WA','TLE','MLE','RE','CE']" :key="v" :value="v" :label="v" />
          </el-select>
          <el-button size="small" @click="sQuery.page = 1; loadSubmissions()">筛选</el-button>
          <el-button size="small" type="primary" plain @click="announceVisible = true">发公告</el-button>
        </div>
      </template>
      <el-table :data="subs" size="small" stripe v-loading="subsLoading">
        <el-table-column prop="username" label="用户" width="130" />
        <el-table-column prop="problem_title" label="题目" min-width="160" />
        <el-table-column label="判定" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.verdict === 'AC' ? 'success' : 'danger'" effect="plain">
              {{ row.verdict }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用例" width="80">
          <template #default="{ row }">{{ row.passed_cases }}/{{ row.total_cases }}</template>
        </el-table-column>
        <el-table-column prop="language" label="语言" width="80" />
        <el-table-column prop="runtime_ms" label="耗时(ms)" width="90" />
        <el-table-column prop="judged_by" label="判定方式" width="100" />
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="rejudge(row)">重判</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="sQuery.page"
        :page-size="sQuery.size"
        :total="subsTotal"
        layout="prev, pager, next"
        style="margin-top: 10px; justify-content: flex-end"
        @current-change="loadSubmissions"
      />
    </el-card>

    <!-- 题目管理（一站式：建题即配用例） -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>题目管理</span>
          <el-tag size="small" effect="plain" type="info">共 {{ problemsTotal }} 题</el-tag>
          <div class="flex-spacer" />
          <el-input
            v-model="pQuery.keyword"
            placeholder="搜索标题"
            size="small"
            style="width: 180px"
            clearable
            @keyup.enter="loadProblems"
          />
          <el-button size="small" @click="loadProblems">搜索</el-button>
          <el-button size="small" type="primary" @click="createVisible = true">新建题目</el-button>
        </div>
      </template>
      <el-table :data="problems" size="small" stripe v-loading="problemsLoading">
        <el-table-column prop="title" label="标题" min-width="200" />
        <el-table-column prop="difficulty" label="难度" width="110" />
        <el-table-column label="标签" min-width="140">
          <template #default="{ row }">
            <el-tag v-for="t in row.tags" :key="t" size="small" effect="plain" style="margin-right: 4px">{{ t }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用例" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.case_count > 0 ? 'success' : 'danger'" effect="plain">
              {{ row.case_count }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="submit_count" label="提交" width="70" />
        <el-table-column prop="accepted_count" label="AC" width="70" />
        <el-table-column prop="time_limit_ms" label="时限(ms)" width="90" />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="$router.push(`/problems/${row.id}`)">预览</el-button>
            <el-button link type="warning" size="small" @click="openCases(row)">用例</el-button>
            <el-button link type="danger" size="small" @click="removeProblem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="pQuery.page"
        :page-size="pQuery.size"
        :total="problemsTotal"
        layout="prev, pager, next"
        style="margin-top: 10px; justify-content: flex-end"
        @current-change="loadProblems"
      />
    </el-card>

    <!-- 建题对话框：题面 + 用例一站式 -->
    <el-dialog v-model="createVisible" title="新建题目（建完即可判题）" width="720px">
      <el-form label-width="90px">
        <el-form-item label="标题"><el-input v-model="createForm.title" /></el-form-item>
        <el-form-item label="题面">
          <el-input v-model="createForm.statement" type="textarea" :rows="5" />
        </el-form-item>
        <el-row :gutter="10">
          <el-col :span="12"><el-form-item label="输入描述"><el-input v-model="createForm.input_desc" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="输出描述"><el-input v-model="createForm.output_desc" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="数据范围"><el-input v-model="createForm.constraints" /></el-form-item>
        <el-row :gutter="10">
          <el-col :span="12"><el-form-item label="标签"><el-input v-model="createForm.tags" placeholder="逗号分隔，如：贪心,排序" /></el-form-item></el-col>
          <el-col :span="12"><el-form-item label="难度">
            <el-select v-model="createForm.difficulty">
              <el-option v-for="d in ['入门','普及-','普及/提高-','普及+/提高','提高+/省选']" :key="d" :value="d" :label="d" />
            </el-select>
          </el-form-item></el-col>
        </el-row>
        <el-form-item label="测试用例">
          <el-input
            v-model="createForm.casesText"
            type="textarea"
            :rows="6"
            placeholder="每行一条，'输入 x' 与 '输出 x' 成对；空行分隔多个用例"
          />
          <div style="font-size: 12px; color: #909399">用例全部为隐藏判题数据，不会展示给学生</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createProblem">创建</el-button>
      </template>
    </el-dialog>
    <!-- 用户详情对话框 -->
    <el-dialog v-model="detailVisible" title="用户详情" width="680px">
      <template v-if="userDetail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="用户名">{{ userDetail.username }}</el-descriptions-item>
          <el-descriptions-item label="昵称">{{ userDetail.nickname }}</el-descriptions-item>
          <el-descriptions-item label="角色">{{ userDetail.role }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ userDetail.is_active ? '正常' : '停用' }}</el-descriptions-item>
          <el-descriptions-item label="等级">{{ userDetail.level }}</el-descriptions-item>
          <el-descriptions-item label="每日目标">{{ userDetail.daily_goal }}</el-descriptions-item>
          <el-descriptions-item label="提交 / AC">
            {{ userDetail.stats.submissions }} / {{ userDetail.stats.ac_count }}
          </el-descriptions-item>
          <el-descriptions-item label="复习项 / 会话">
            {{ userDetail.stats.review_items }} / {{ userDetail.stats.chat_sessions }}
          </el-descriptions-item>
          <el-descriptions-item label="最近登录">{{ userDetail.last_login_at || '-' }}</el-descriptions-item>
          <el-descriptions-item label="注册时间">{{ fmtDate(userDetail.created_at) }}</el-descriptions-item>
        </el-descriptions>
        <el-divider content-position="left">最近 10 次提交</el-divider>
        <el-table :data="userDetail.recent_submissions" size="small">
          <el-table-column prop="problem_id" label="题目" min-width="180" />
          <el-table-column label="判定" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.verdict === 'AC' ? 'success' : 'danger'" effect="plain">
                {{ row.verdict }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="language" label="语言" width="90" />
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </el-dialog>

    <!-- 用例编辑器 -->
    <el-dialog v-model="casesVisible" :title="`测试用例 · ${casesProblem?.title ?? ''}`" width="720px">
      <el-alert type="info" :closable="false" style="margin-bottom: 10px"
        title="格式：每个用例写「输入 xxx」与「输出 xxx」两行，用例之间空一行。保存后立即生效（可配合提交管理里的重判）。" />
      <el-input
        v-model="casesText"
        type="textarea"
        :rows="14"
        class="mono"
        v-loading="casesLoading"
        placeholder="输入 1 2&#10;输出 3&#10;&#10;输入 5 5&#10;输出 10"
      />
      <template #footer>
        <el-button @click="casesVisible = false">取消</el-button>
        <el-button type="primary" :loading="casesSaving" @click="saveCases">保存全部用例</el-button>
      </template>
    </el-dialog>

    <!-- 审计日志 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>操作审计</span>
          <el-tag size="small" effect="plain" type="info">共 {{ auditTotal }} 条</el-tag>
          <div class="flex-spacer" />
          <el-select v-model="aQuery.action" size="small" style="width: 130px" @change="aQuery.page = 1; loadAudit()">
            <el-option v-for="a in AUDIT_ACTIONS" :key="a.v" :value="a.v" :label="a.l" />
          </el-select>
          <el-input v-model="aQuery.actor" placeholder="操作人" size="small" style="width: 120px" clearable />
          <el-button size="small" @click="aQuery.page = 1; loadAudit()">筛选</el-button>
        </div>
      </template>
      <el-table :data="auditLogs" size="small" stripe v-loading="auditLoading">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="actor_name" label="操作人" width="120" />
        <el-table-column label="动作" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.action_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="对象" min-width="180">
          <template #default="{ row }">
            <span class="stat-label">{{ row.target_type }} · {{ row.target_id || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="240">
          <template #default="{ row }">
            <span class="stat-label">{{ JSON.stringify(row.detail).slice(0, 110) }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="aQuery.page"
        :page-size="aQuery.size"
        :total="auditTotal"
        layout="prev, pager, next"
        style="margin-top: 10px; justify-content: flex-end"
        @current-change="loadAudit"
      />
    </el-card>

    <!-- 公告对话框 -->
    <el-dialog v-model="announceVisible" title="发送站内公告" width="560px">
      <el-form label-width="80px">
        <el-form-item label="标题"><el-input v-model="announceForm.title" /></el-form-item>
        <el-form-item label="内容">
          <el-input v-model="announceForm.body" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="跳转链接">
          <el-input v-model="announceForm.link" placeholder="可选，如 /problems" />
        </el-form-item>
        <el-form-item label="收件人">
          <el-input v-model="announceForm.username" placeholder="留空 = 全员广播" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="announceVisible = false">取消</el-button>
        <el-button type="primary" :loading="announcing" @click="sendAnnounce">发送</el-button>
      </template>
    </el-dialog>
  </div>
</template>
