<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { masteryApi, reviewApi } from '@/api'
import type { MasteryOverview, MasteryProfileRow, TimelinePoint } from '@/api/types'
import { levelColor, levelText } from '@/utils/format'

const loading = ref(false)
const overview = ref<MasteryOverview | null>(null)
const profile = ref<MasteryProfileRow[]>([])
const timeline = ref<TimelinePoint[]>([])
const windowDays = ref(30)
const recalculating = ref(false)

const radarEl = ref<HTMLDivElement | null>(null)
const timelineEl = ref<HTMLDivElement | null>(null)
const curveEl = ref<HTMLDivElement | null>(null)

const radarChart = shallowRef<echarts.ECharts | null>(null)
const timelineChart = shallowRef<echarts.ECharts | null>(null)
const curveChart = shallowRef<echarts.ECharts | null>(null)

async function load() {
  loading.value = true
  try {
    const [o, p, t] = await Promise.allSettled([
      masteryApi.overview(windowDays.value),
      masteryApi.profile(),
      masteryApi.timeline(60),
    ])
    if (o.status === 'fulfilled') overview.value = o.value
    if (p.status === 'fulfilled') profile.value = p.value
    if (t.status === 'fulfilled') timeline.value = t.value.items
    renderRadar()
    renderTimeline()
    void renderCurve()
  } finally {
    loading.value = false
  }
}

/** 雷达图：最多展示 8 个知识点，维度太少时用中性值补齐 */
function renderRadar() {
  if (!radarEl.value) return
  if (!radarChart.value) radarChart.value = echarts.init(radarEl.value)

  const rows = profile.value.slice(0, 8)
  if (!rows.length) {
    radarChart.value.clear()
    return
  }
  radarChart.value.setOption({
    tooltip: {},
    radar: {
      indicator: rows.map((r) => ({ name: r.tag, max: 100 })),
      radius: '66%',
      splitNumber: 4,
      axisName: { color: '#606266', fontSize: 12 },
      splitArea: { areaStyle: { color: ['#fff', '#fafafa'] } },
    },
    series: [
      {
        type: 'radar',
        data: [
          {
            value: rows.map((r) => Number(r.score.toFixed(1))),
            name: '掌握度',
            areaStyle: { color: 'rgba(64,158,255,0.25)' },
            lineStyle: { color: '#409EFF', width: 2 },
            itemStyle: { color: '#409EFF' },
          },
        ],
      },
    ],
  })
}

/** 时间序列：学习状态评分变化 */
function renderTimeline() {
  if (!timelineEl.value) return
  if (!timelineChart.value) timelineChart.value = echarts.init(timelineEl.value)

  if (!timeline.value.length) {
    timelineChart.value.clear()
    return
  }
  timelineChart.value.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 44, right: 20, top: 30, bottom: 30 },
    xAxis: {
      type: 'category',
      data: timeline.value.map((p) => p.created_at.slice(5, 16).replace('T', ' ')),
      axisLabel: { fontSize: 11, color: '#909399' },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { fontSize: 11, color: '#909399' },
      splitLine: { lineStyle: { color: '#f0f0f0' } },
    },
    series: [
      {
        name: '学习状态',
        type: 'line',
        smooth: true,
        data: timeline.value.map((p) => p.score),
        lineStyle: { color: '#67C23A', width: 2 },
        itemStyle: { color: '#67C23A' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(103,194,58,0.35)' },
            { offset: 1, color: 'rgba(103,194,58,0.02)' },
          ]),
        },
      },
    ],
  })
}

/** 遗忘曲线：取最紧急的一条待复习项，展示未来 30 天保持率 */
async function renderCurve() {
  if (!curveEl.value) return
  if (!curveChart.value) curveChart.value = echarts.init(curveEl.value)

  try {
    const due = await reviewApi.due(1)
    if (!due.due.length) {
      curveChart.value.clear()
      return
    }
    // 用 mark 接口会改动数据，这里改为按 stability 直接画：调用一次 queue 拿 stability
    const q = await reviewApi.queue({ limit: 50 })
    const item = q.items[0]
    if (!item) {
      curveChart.value.clear()
      return
    }
    const decay = Math.log(0.9)
    const points: Array<[number, number]> = []
    for (let d = 0; d <= 30; d += 1) {
      const r = Math.exp((decay * d) / Math.max(0.1, item.stability))
      points.push([d, Number((r * 100).toFixed(1))])
    }

    curveChart.value.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: (params: any) => `第 ${params[0].value[0]} 天<br/>保持率 ${params[0].value[1]}%`,
      },
      grid: { left: 44, right: 20, top: 30, bottom: 40 },
      xAxis: {
        type: 'value',
        name: '天数',
        nameTextStyle: { fontSize: 11, color: '#909399' },
        axisLabel: { fontSize: 11, color: '#909399' },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        name: '保持率 %',
        nameTextStyle: { fontSize: 11, color: '#909399' },
        axisLabel: { fontSize: 11, color: '#909399' },
        splitLine: { lineStyle: { color: '#f0f0f0' } },
      },
      series: [
        {
          type: 'line',
          smooth: true,
          showSymbol: false,
          data: points,
          lineStyle: { color: '#E6A23C', width: 2 },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [{ yAxis: 90, lineStyle: { color: '#67C23A', type: 'dashed' }, label: { formatter: '目标 90%' } }],
          },
        },
      ],
    })
  } catch {
    curveChart.value?.clear()
  }
}

async function recalc() {
  recalculating.value = true
  try {
    await masteryApi.recalc(windowDays.value)
    ElMessage.success('已重算掌握度并写入快照')
    await load()
  } finally {
    recalculating.value = false
  }
}

function resizeAll() {
  radarChart.value?.resize()
  timelineChart.value?.resize()
  curveChart.value?.resize()
}

watch(windowDays, () => {
  void load()
})

onMounted(() => {
  void load()
  window.addEventListener('resize', resizeAll)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAll)
  radarChart.value?.dispose()
  timelineChart.value?.dispose()
  curveChart.value?.dispose()
})
</script>

<template>
  <div v-loading="loading">
    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <span>统计窗口</span>
        <el-select v-model="windowDays" style="width: 130px">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
          <el-option :value="365" label="近 365 天" />
        </el-select>
        <div class="flex-spacer" />
        <el-button :loading="recalculating" @click="recalc">
          <el-icon><Refresh /></el-icon> 重算画像
        </el-button>
      </div>
    </el-card>

    <!-- 总览 -->
    <el-row :gutter="16">
      <el-col :xs="24" :md="8">
        <el-card class="page-card" shadow="never">
          <div class="score-box">
            <el-progress
              type="dashboard"
              :percentage="Math.round(overview?.score ?? 0)"
              :width="150"
              :stroke-width="12"
              :color="levelColor(overview?.level || 'L1')"
            >
              <template #default>
                <div class="dashboard-score">{{ (overview?.score ?? 0).toFixed(1) }}</div>
                <div class="stat-label">学习状态</div>
              </template>
            </el-progress>
            <div style="margin-top: 10px">
              <el-tag :style="{ background: levelColor(overview?.level || 'L1'), color: '#fff', border: 'none' }">
                {{ overview?.level }} · {{ levelText(overview?.level || 'L1') }}
              </el-tag>
            </div>
            <div class="stat-label" style="margin-top: 8px">
              置信度 {{ ((overview?.confidence ?? 0) * 100).toFixed(0) }}%
              <el-tooltip content="样本越多置信度越高，样本不足时评分会向中性 50 分收缩">
                <el-icon style="vertical-align: -2px; margin-left: 4px"><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="16">
        <el-card class="page-card" shadow="never">
          <template #header>强弱项</template>
          <el-row :gutter="16">
            <el-col :span="12">
              <div class="stat-label" style="margin-bottom: 8px">薄弱知识点（优先补）</div>
              <div v-if="overview?.weak_tags?.length">
                <el-tag
                  v-for="t in overview.weak_tags"
                  :key="t"
                  type="danger"
                  effect="plain"
                  style="margin: 0 6px 6px 0"
                >
                  {{ t }}
                </el-tag>
              </div>
              <div v-else class="stat-label">暂无数据</div>
            </el-col>
            <el-col :span="12">
              <div class="stat-label" style="margin-bottom: 8px">优势知识点</div>
              <div v-if="overview?.strong_tags?.length">
                <el-tag
                  v-for="t in overview.strong_tags"
                  :key="t"
                  type="success"
                  effect="plain"
                  style="margin: 0 6px 6px 0"
                >
                  {{ t }}
                </el-tag>
              </div>
              <div v-else class="stat-label">暂无数据</div>
            </el-col>
          </el-row>
          <el-alert v-if="overview?.suggestion" type="info" :closable="false" style="margin-top: 12px">
            <div class="prose">{{ overview.suggestion }}</div>
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>知识点雷达图（最弱 8 项）</template>
          <div ref="radarEl" style="height: 320px" />
          <div v-if="!profile.length" class="empty-hint">暂无画像数据，先完成几道题</div>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>遗忘曲线（最紧急一项，未来 30 天）</template>
          <div ref="curveEl" style="height: 320px" />
        </el-card>
      </el-col>
    </el-row>

    <el-card class="page-card" shadow="never">
      <template #header>学习状态时间序列</template>
      <div ref="timelineEl" style="height: 280px" />
      <div v-if="!timeline.length" class="empty-hint">暂无快照，点击"重算画像"生成第一个快照</div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <template #header>知识点明细（按掌握度升序）</template>
      <el-table :data="profile" size="small" stripe>
        <el-table-column prop="tag" label="知识点" min-width="140" />
        <el-table-column label="掌握度" width="240">
          <template #default="{ row }">
            <el-progress
              :percentage="Math.round(row.score)"
              :stroke-width="12"
              :color="row.score < 40 ? '#F56C6C' : row.score < 60 ? '#E6A23C' : '#67C23A'"
            />
          </template>
        </el-table-column>
        <el-table-column label="分数" width="90">
          <template #default="{ row }">{{ row.score.toFixed(1) }}</template>
        </el-table-column>
        <el-table-column label="置信度" width="100">
          <template #default="{ row }">{{ (row.confidence * 100).toFixed(0) }}%</template>
        </el-table-column>
        <el-table-column prop="attempts" label="尝试" width="80" />
        <el-table-column prop="ac_count" label="通过" width="80" />
        <el-table-column label="等级" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">暂无数据</div>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.score-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 8px 0;
}

.dashboard-score {
  font-size: 26px;
  font-weight: 700;
  color: #303133;
}
</style>
