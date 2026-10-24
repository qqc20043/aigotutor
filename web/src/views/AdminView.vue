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

onMounted(() => {
  void load()
  void loadProblems()
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
          <span class="stat-label">共 {{ userTotal }} 人</span>
        </div>
      </template>

      <el-table :data="users" size="small" stripe>
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
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="$router.push(`/problems/${row.id}`)">预览</el-button>
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
  </div>
</template>
