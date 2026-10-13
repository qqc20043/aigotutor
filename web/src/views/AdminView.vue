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

onMounted(load)
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
  </div>
</template>
