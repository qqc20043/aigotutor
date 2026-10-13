<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { checkinApi, leaderboardApi, reportApi } from '@/api'
import { errorText } from '@/api/http'
import { useUserStore } from '@/stores/user'

const user = useUserStore()

const loading = ref(false)
const checking = ref(false)
const status = ref<Record<string, any> | null>(null)
const board = ref<Record<string, any> | null>(null)
const metric = ref('ac')
const window_ = ref('all')

const note = ref('')

const today = computed(() => status.value?.today || {})
const streak = computed(() => status.value?.streak || {})
const history = computed<Array<Record<string, any>>>(() => status.value?.history || [])

/** 最近 8 周打卡热力图数据 */
const heatmap = computed(() => {
  const map = new Map<string, Record<string, any>>()
  for (const h of history.value) map.set(h.date, h)
  const cells: Array<{ date: string; level: number; ac: number; goal: boolean }> = []
  const today = new Date()
  for (let i = 55; i >= 0; i--) {
    const d = new Date(today)
    d.setDate(d.getDate() - i)
    const key = d.toISOString().slice(0, 10)
    const row = map.get(key)
    const ac = row?.ac_count || 0
    let level = 0
    if (ac > 0) level = 1
    if (ac >= 3) level = 2
    if (ac >= 6) level = 3
    if (ac >= 10) level = 4
    cells.push({ date: key, level, ac, goal: Boolean(row?.goal_met) })
  }
  return cells
})

const goalProgress = computed(() => {
  const done = today.value.ac_count || 0
  const goal = status.value?.daily_goal || 3
  return Math.min(100, Math.round((done / Math.max(1, goal)) * 100))
})

async function load() {
  loading.value = true
  try {
    const [s, b] = await Promise.allSettled([
      checkinApi.status(),
      leaderboardApi.get({ metric: metric.value, window: window_.value, limit: 20 }),
    ])
    if (s.status === 'fulfilled') status.value = s.value
    if (b.status === 'fulfilled') board.value = b.value
  } finally {
    loading.value = false
  }
}

async function doCheckin() {
  checking.value = true
  try {
    const res = await checkinApi.do(note.value)
    ElMessage.success(
      res.goal_met
        ? `打卡成功，今日目标已达成（连续 ${res.streak.current} 天）`
        : `打卡成功，今日 ${res.ac_count}/${res.daily_goal}，继续加油`,
    )
    note.value = ''
    await load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    checking.value = false
  }
}

async function loadBoard() {
  board.value = await leaderboardApi.get({ metric: metric.value, window: window_.value, limit: 20 })
}

async function downloadReport() {
  try {
    const md = await reportApi.markdown(7)
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `aigotutor-report-${new Date().toISOString().slice(0, 10)}.md`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('报告已下载')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function levelColor(level: number): string {
  return ['#ebeef5', '#c6e2ff', '#79bbff', '#409eff', '#1f5fbf'][level] || '#ebeef5'
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <!-- 打卡 -->
    <el-row :gutter="16">
      <el-col :xs="24" :md="10">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; gap: 8px">
              <span>每日打卡</span>
              <el-tag size="small" effect="plain">{{ status?.timezone || 'Asia/Shanghai' }}</el-tag>
              <div class="flex-spacer" />
              <el-tag v-if="streak.checked_today" size="small" type="success" effect="plain">
                今日已打卡
              </el-tag>
            </div>
          </template>

          <div class="streak-box">
            <el-statistic title="当前连击" :value="streak.current || 0" suffix="天" />
            <el-statistic title="历史最长" :value="streak.longest || 0" suffix="天" />
            <el-statistic title="累计打卡" :value="streak.total_days || 0" suffix="天" />
          </div>

          <el-divider content-position="left">今日进度</el-divider>
          <div class="goal-row">
            <span class="stat-label">通过 {{ today.ac_count || 0 }} / {{ status?.daily_goal || 3 }}</span>
            <el-progress
              :percentage="goalProgress"
              :stroke-width="12"
              :status="goalProgress >= 100 ? 'success' : undefined"
              style="flex: 1"
            />
          </div>
          <div class="stat-label" style="margin-top: 6px">
            提交 {{ today.submit_count || 0 }} 次 · 涉及 {{ today.distinct_problems || 0 }} 道题
          </div>

          <div class="toolbar" style="margin-top: 14px; margin-bottom: 0">
            <el-input v-model="note" placeholder="今日感想（可选）" size="small" style="width: 180px" />
            <el-button type="primary" size="small" :loading="checking" @click="doCheckin">
              <el-icon><Check /></el-icon> 打卡
            </el-button>
            <el-button size="small" @click="downloadReport">
              <el-icon><Download /></el-icon> 下载周报
            </el-button>
          </div>

          <el-alert type="info" :closable="false" style="margin-top: 12px">
            <div class="stat-label" style="line-height: 1.7">
              日界按你的本地时区（{{ status?.timezone }}）计算 —— 否则凌晨打卡会被算进前一天，
              连击会莫名中断。
            </div>
          </el-alert>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="14">
        <el-card class="page-card" shadow="never">
          <template #header>打卡日历（近 8 周）</template>
          <div class="heatmap">
            <el-tooltip
              v-for="c in heatmap"
              :key="c.date"
              :content="`${c.date}：通过 ${c.ac} 题${c.goal ? '（达成目标）' : ''}`"
              placement="top"
            >
              <div class="heat-cell" :style="{ background: levelColor(c.level) }" />
            </el-tooltip>
          </div>
          <div class="heat-legend">
            <span class="stat-label">少</span>
            <div v-for="l in [0, 1, 2, 3, 4]" :key="l" class="heat-cell" :style="{ background: levelColor(l) }" />
            <span class="stat-label">多</span>
          </div>

          <el-divider content-position="left">最近打卡记录</el-divider>
          <el-table :data="history.slice(0, 8)" size="small">
            <el-table-column prop="date" label="日期" width="120" />
            <el-table-column prop="ac_count" label="通过" width="80" />
            <el-table-column prop="submit_count" label="提交" width="80" />
            <el-table-column label="目标" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.goal_met" size="small" type="success" effect="plain">达成</el-tag>
                <span v-else class="stat-label">未达成</span>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="感想" min-width="140" />
            <template #empty>
              <div class="empty-hint">还没有打卡记录</div>
            </template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 排行榜 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>排行榜</span>
          <el-select v-model="metric" size="small" style="width: 130px" @change="loadBoard">
            <el-option label="通过题数" value="ac" />
            <el-option label="提交次数" value="submit" />
          </el-select>
          <el-select v-model="window_" size="small" style="width: 130px" @change="loadBoard">
            <el-option label="全部时间" value="all" />
            <el-option label="近 7 天" value="week" />
            <el-option label="近 30 天" value="month" />
          </el-select>
          <div class="flex-spacer" />
          <el-tag v-if="board?.my_rank" size="small" type="success" effect="plain">
            我的名次：第 {{ board.my_rank }} 名
          </el-tag>
          <el-button size="small" @click="loadBoard">
            <el-icon><Refresh /></el-icon> 刷新
          </el-button>
        </div>
      </template>

      <el-alert v-if="board?.note" type="info" :closable="false" style="margin-bottom: 12px">
        <span class="stat-label">{{ board.note }}</span>
      </el-alert>

      <el-table :data="board?.items || []" size="small" stripe>
        <el-table-column label="名次" width="80">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.rank <= 3 ? 'success' : 'info'"
              effect="plain"
            >
              {{ row.rank }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用户" min-width="160">
          <template #default="{ row }">
            {{ row.nickname }}
            <span v-if="row.user_id === user.user?.id" class="stat-label">（我）</span>
          </template>
        </el-table-column>
        <el-table-column label="数值" width="120">
          <template #default="{ row }">
            <b>{{ row.value }}</b>
            <span class="stat-label">{{ metric === 'ac' ? ' 题' : ' 次' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">暂无排行数据</div>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.streak-box {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.goal-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.heatmap {
  display: grid;
  grid-template-columns: repeat(14, 1fr);
  gap: 4px;
}

.heat-cell {
  width: 100%;
  aspect-ratio: 1;
  border-radius: 3px;
  background: #ebeef5;
}

.heat-legend {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 10px;
  justify-content: flex-end;
}

.heat-legend .heat-cell {
  width: 14px;
  height: 14px;
}
</style>
