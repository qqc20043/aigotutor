<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import * as echarts from 'echarts'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { practiceApi, reviewApi, type PracticeItem } from '@/api'
import type { ReviewItem, ReviewMarkResult } from '@/api/types'
import { difficultyType, fmtRelative, retentionColor } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const dueOnly = ref(true)
const items = ref<ReviewItem[]>([])
const forecast = ref<Array<{ date: string; count: number }>>([])
const dueCount = ref(0)

const marking = ref<string>('')
const lastResult = ref<ReviewMarkResult | null>(null)

/** 复习队列优先 Agent：在客观到期队列之上给出"先做哪个"的推荐顺序 */
const priorityLoading = ref(false)
const priority = ref<{
  ordered: Array<{ problem_id: string; title: string; order: number; reason: string; urgency: string }>
  strategy: string
  degraded?: boolean
} | null>(null)
const timeBudget = ref(60)

async function loadPriority() {
  priorityLoading.value = true
  try {
    const res = await reviewApi.priority({ limit: 20, time_budget_minutes: timeBudget.value })
    const data = res.data
    if (!data || !data.ordered) {
      priority.value = null
      ElMessage.info(res.message || '当前没有到期的复习项')
    } else {
      priority.value = {
        ordered: data.ordered,
        strategy: data.strategy,
        degraded: res.degraded,
      }
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取推荐顺序失败')
  } finally {
    priorityLoading.value = false
  }
}

const forecastEl = ref<HTMLDivElement | null>(null)
const forecastChart = shallowRef<echarts.ECharts | null>(null)

const RATINGS = [
  { value: 1, label: '完全忘了', type: 'danger', tip: '稳定度回退，很快再出现' },
  { value: 2, label: '有点难', type: 'warning', tip: '间隔小幅增长' },
  { value: 3, label: '还行', type: 'primary', tip: '正常增长' },
  { value: 4, label: '很简单', type: 'success', tip: '间隔大幅增长' },
] as const

const summary = computed(() => {
  const total = items.value.length
  const critical = items.value.filter((i) => i.retrievability < 0.6).length
  const overdue = items.value.filter((i) => i.overdue_days > 3).length
  return { total, critical, overdue }
})

async function load() {
  loading.value = true
  try {
    const [q, d] = await Promise.allSettled([
      reviewApi.queue({ limit: 50, due_only: dueOnly.value }),
      reviewApi.due(20),
    ])
    if (q.status === 'fulfilled') {
      items.value = q.value.items
      dueCount.value = q.value.due_count
    }
    if (d.status === 'fulfilled') {
      forecast.value = d.value.forecast
      renderForecast()
    }
  } finally {
    loading.value = false
  }
}

function renderForecast() {
  if (!forecastEl.value) return
  if (!forecastChart.value) forecastChart.value = echarts.init(forecastEl.value)
  if (!forecast.value.length) {
    forecastChart.value.clear()
    return
  }
  forecastChart.value.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 24, bottom: 40 },
    xAxis: {
      type: 'category',
      data: forecast.value.map((f) => f.date.slice(5)),
      axisLabel: { fontSize: 10, color: '#909399', interval: 4 },
    },
    yAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#909399' },
      splitLine: { lineStyle: { color: '#f0f0f0' } },
    },
    series: [
      {
        type: 'bar',
        data: forecast.value.map((f) => f.count),
        itemStyle: { color: '#409EFF', borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 18,
      },
    ],
  })
}

async function mark(problemId: string, rating: number) {
  marking.value = problemId
  try {
    const res = await reviewApi.mark(problemId, rating)
    lastResult.value = res
    ElMessage.success(
      `已记录：下次复习在 ${res.next_interval_days.toFixed(1)} 天后（稳定度 ${res.schedule.stability.toFixed(2)}）`,
    )
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    marking.value = ''
  }
}

function goPractice(problemId: string) {
  void router.push(`/problems/${problemId}`)
}

// ---- 今日混排（智能练习）：三路采题 + 相邻不同知识点 ----
const mixLoading = ref(false)
const mixItems = ref<PracticeItem[]>([])
const mixStats = ref<{ size: number; interleaved_pairs: number; sources: Record<string, number> } | null>(null)
const SOURCE_META: Record<string, { label: string; type: 'warning' | 'danger' | 'info' }> = {
  review: { label: 'FSRS 到期', type: 'warning' },
  leech: { label: '顽固错题', type: 'danger' },
  fresh: { label: '新题', type: 'info' },
}

async function loadMix() {
  mixLoading.value = true
  try {
    const out = await practiceApi.interleaved({ size: 6 })
    mixItems.value = out.items
    mixStats.value = out.stats
  } catch {
    ElMessage.error('生成混排练习失败')
  } finally {
    mixLoading.value = false
  }
}

onMounted(() => {
  void load()
  void loadMix()
})
</script>

<template>
  <div v-loading="loading">
    <el-card class="page-card" shadow="never" style="margin-bottom: 14px">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>今日混排 · 智能练习</span>
          <el-tag v-if="mixStats" size="small" effect="plain" type="info">
            {{ mixStats.size }} 题 · 交错 {{ mixStats.interleaved_pairs }} 对 · 相邻不同知识点
          </el-tag>
          <div class="flex-spacer" />
          <el-button size="small" :loading="mixLoading" @click="loadMix">重新生成</el-button>
        </div>
      </template>
      <el-alert
        type="info"
        :closable="false"
        style="margin-bottom: 12px"
        title="来源：FSRS 到期复习 + 顽固错题专项 + 薄弱知识点新题，按知识点交错排列（混排比集中刷同类题留存更好）"
      />
      <div class="mix-list">
        <div
          v-for="item in mixItems"
          :key="item.problem_id"
          class="mix-item"
          @click="router.push(`/problems/${item.problem_id}`)"
        >
          <span class="mix-order">{{ item.order }}</span>
          <el-tag size="small" :type="SOURCE_META[item.source]?.type || 'info'" effect="plain">
            {{ SOURCE_META[item.source]?.label || item.source }}
          </el-tag>
          <span class="mix-title">{{ item.title }}</span>
          <span class="mix-reason">{{ item.reason }}</span>
        </div>
        <el-empty v-if="!mixItems.length && !mixLoading" description="暂无可练习的题" :image-size="60" />
      </div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <el-statistic title="待复习总数" :value="dueCount" style="margin-right: 28px" />
        <el-statistic title="保持率低于 60%" :value="summary.critical" style="margin-right: 28px" />
        <el-statistic title="逾期超 3 天" :value="summary.overdue" />
        <div class="flex-spacer" />
        <el-input-number
          v-model="timeBudget"
          :min="0"
          :max="600"
          :step="15"
          size="small"
          controls-position="right"
          style="width: 130px"
          title="今日时间预算（分钟），0 表示不限"
        />
        <el-button :loading="priorityLoading" @click="loadPriority">
          <el-icon><Sort /></el-icon> 今日先做哪几道
        </el-button>
        <el-switch
          v-model="dueOnly"
          active-text="只看已到期"
          @change="load"
        />
        <el-button @click="load">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </el-card>

    <!-- 复习队列优先 Agent：按学习收益重排 + 解释为什么是这个顺序 -->
    <el-card v-if="priority" class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>今日推荐顺序</span>
          <el-tag v-if="priority.degraded" type="info" size="small" effect="plain">规则引擎</el-tag>
          <div class="flex-spacer" />
          <el-button link size="small" @click="priority = null">收起</el-button>
        </div>
      </template>

      <el-alert type="success" :closable="false" style="margin-bottom: 12px">
        {{ priority.strategy }}
      </el-alert>

      <el-table :data="priority.ordered" size="small" stripe>
        <el-table-column label="#" width="60">
          <template #default="{ row }">
            <el-tag size="small" :type="row.order === 1 ? 'danger' : row.order <= 3 ? 'warning' : 'info'">
              {{ row.order }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="题目" min-width="160">
          <template #default="{ row }">
            <span class="title-link" @click="goPractice(row.problem_id)">{{ row.title || row.problem_id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="紧迫度" width="120">
          <template #default="{ row }">
            <span class="stat-label">{{ row.urgency }}</span>
          </template>
        </el-table-column>
        <el-table-column label="为什么排这里" min-width="240">
          <template #default="{ row }">
            <span style="font-size: 13px">{{ row.reason }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :md="16">
        <el-card class="page-card" shadow="never">
          <template #header>
            复习队列
            <span class="stat-label" style="margin-left: 8px">
              保持率由 FSRS 计算，越低越该复习
            </span>
          </template>

          <el-table :data="items" size="small" stripe>
            <el-table-column label="题目" min-width="180">
              <template #default="{ row }">
                <span class="title-link" @click="goPractice(row.problem_id)">{{ row.title }}</span>
              </template>
            </el-table-column>

            <el-table-column label="难度" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="difficultyType(row.difficulty)" effect="plain">
                  {{ row.difficulty }}
                </el-tag>
              </template>
            </el-table-column>

            <el-table-column label="保持率" width="150">
              <template #default="{ row }">
                <div style="display: flex; align-items: center; gap: 6px">
                  <el-progress
                    :percentage="Math.round(row.retrievability * 100)"
                    :stroke-width="10"
                    :show-text="false"
                    :color="retentionColor(row.retrievability)"
                    style="flex: 1"
                  />
                  <span class="stat-label">{{ (row.retrievability * 100).toFixed(0) }}%</span>
                </div>
              </template>
            </el-table-column>

            <el-table-column label="逾期" width="110">
              <template #default="{ row }">
                <el-tag v-if="row.overdue_days > 0" size="small" type="danger" effect="plain">
                  {{ row.overdue_days.toFixed(1) }} 天
                </el-tag>
                <span v-else class="stat-label">未到期</span>
              </template>
            </el-table-column>

            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <span class="stat-label">
                  S={{ row.stability.toFixed(1) }} · {{ row.reps }} 次
                </span>
              </template>
            </el-table-column>

            <el-table-column label="复习反馈" width="290" fixed="right">
              <template #default="{ row }">
                <div class="rating-group">
                  <el-tooltip
                    v-for="r in RATINGS"
                    :key="r.value"
                    :content="r.tip"
                    placement="top"
                  >
                    <el-button
                      size="small"
                      :type="r.type"
                      plain
                      :loading="marking === row.problem_id"
                      @click="mark(row.problem_id, r.value)"
                    >
                      {{ r.label }}
                    </el-button>
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>

            <template #empty>
              <div class="empty-hint">
                {{ dueOnly ? '今天没有需要复习的题目 🎉' : '复习队列为空，先做几道题' }}
              </div>
            </template>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="8">
        <el-card class="page-card" shadow="never">
          <template #header>未来 30 天复习量预测</template>
          <div ref="forecastEl" style="height: 200px" />
        </el-card>

        <el-card v-if="lastResult" class="page-card" shadow="never">
          <template #header>最近一次调度结果</template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="新稳定度">
              {{ lastResult.schedule.stability.toFixed(3) }} 天
            </el-descriptions-item>
            <el-descriptions-item label="难度 D">
              {{ lastResult.schedule.difficulty.toFixed(2) }}
            </el-descriptions-item>
            <el-descriptions-item label="下次间隔">
              {{ lastResult.next_interval_days.toFixed(2) }} 天
            </el-descriptions-item>
            <el-descriptions-item label="下次到期">
              {{ fmtRelative(lastResult.schedule.due_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="累计复习">
              {{ lastResult.schedule.reps }} 次 / 遗忘 {{ lastResult.schedule.lapses }} 次
            </el-descriptions-item>
          </el-descriptions>
          <div class="stat-label" style="margin-top: 10px; line-height: 1.7">
            FSRS 简化版：S（稳定度）、D（难度）、R（可提取性）三参数。
            评分"完全忘了"会回退稳定度并累计遗忘次数。
          </div>
        </el-card>

        <el-card class="page-card" shadow="never">
          <template #header>评分怎么选</template>
          <div class="prose" style="font-size: 13px">
            不用纠结"对错"，只判断记忆状态：
            <br />· <b>完全忘了</b>：看着题目也没有思路
            <br />· <b>有点难</b>：想了一会儿才想起来
            <br />· <b>还行</b>：顺利想出解法
            <br />· <b>很简单</b>：一眼就有思路
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.title-link {
  color: #409eff;
  cursor: pointer;
}

.title-link:hover {
  text-decoration: underline;
}

.rating-group {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
</style>

<style scoped>
.mix-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mix-item {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 12px;
  cursor: pointer;
}
.mix-item:hover {
  border-color: var(--el-color-primary-light-5);
}
.mix-order {
  font-weight: 700;
  width: 20px;
  text-align: center;
}
.mix-title {
  font-weight: 500;
}
.mix-reason {
  font-size: 12px;
  color: #909399;
  margin-left: auto;
}
.flex-spacer {
  flex: 1;
}
</style>
