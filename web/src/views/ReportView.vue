<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { reportApi } from '@/api'
import { errorText } from '@/api/http'

const loading = ref(false)
const days = ref(7)
const report = ref<Record<string, any> | null>(null)
const markdown = ref('')

const dailyEl = ref<HTMLDivElement | null>(null)
const verdictEl = ref<HTMLDivElement | null>(null)
const dailyChart = shallowRef<echarts.ECharts | null>(null)
const verdictChart = shallowRef<echarts.ECharts | null>(null)

async function load() {
  loading.value = true
  try {
    const [json, md] = await Promise.allSettled([
      reportApi.json(days.value),
      reportApi.markdown(days.value),
    ])
    if (json.status === 'fulfilled') {
      report.value = json.value
      renderCharts()
    }
    if (md.status === 'fulfilled') markdown.value = md.value
  } finally {
    loading.value = false
  }
}

function renderCharts() {
  const daily = (report.value?.daily || []) as Array<Record<string, any>>
  if (dailyEl.value) {
    if (!dailyChart.value) dailyChart.value = echarts.init(dailyEl.value)
    dailyChart.value.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: ['提交', '通过'], top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 40, right: 16, top: 34, bottom: 30 },
      xAxis: {
        type: 'category',
        data: daily.map((d) => d.date.slice(5)),
        axisLabel: { fontSize: 10, color: '#909399' },
      },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: '#909399' },
        splitLine: { lineStyle: { color: '#f0f0f0' } },
      },
      series: [
        {
          name: '提交',
          type: 'bar',
          data: daily.map((d) => d.submit),
          itemStyle: { color: '#409EFF', borderRadius: [3, 3, 0, 0] },
          barMaxWidth: 20,
        },
        {
          name: '通过',
          type: 'bar',
          data: daily.map((d) => d.ac),
          itemStyle: { color: '#67C23A', borderRadius: [3, 3, 0, 0] },
          barMaxWidth: 20,
        },
      ],
    })
  }

  const verdicts = (report.value?.verdicts || {}) as Record<string, number>
  if (verdictEl.value) {
    if (!verdictChart.value) verdictChart.value = echarts.init(verdictEl.value)
    const data = Object.entries(verdicts).map(([name, value]) => ({ name, value }))
    if (!data.length) {
      verdictChart.value.clear()
      return
    }
    verdictChart.value.setOption({
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, textStyle: { fontSize: 11 } },
      series: [
        {
          type: 'pie',
          radius: ['40%', '65%'],
          center: ['50%', '45%'],
          data,
          label: { fontSize: 11 },
        },
      ],
    })
  }
}

async function download() {
  try {
    const md = await reportApi.markdown(days.value)
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `aigotutor-report-${days.value}d.md`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('报告已下载')
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

function resizeAll() {
  dailyChart.value?.resize()
  verdictChart.value?.resize()
}

watch(days, load)

onMounted(() => {
  void load()
  window.addEventListener('resize', resizeAll)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeAll)
  dailyChart.value?.dispose()
  verdictChart.value?.dispose()
})
</script>

<template>
  <div v-loading="loading">
    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <span>统计窗口</span>
        <el-select v-model="days" style="width: 130px">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
        </el-select>
        <div class="flex-spacer" />
        <el-button @click="load">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
        <el-button type="primary" @click="download">
          <el-icon><Download /></el-icon> 下载 Markdown
        </el-button>
      </div>
    </el-card>

    <!-- 概览 -->
    <el-row :gutter="16">
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value">{{ report?.summary?.submissions ?? 0 }}</div>
          <div class="stat-label">提交次数</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #67c23a">{{ report?.summary?.ac ?? 0 }}</div>
          <div class="stat-label">通过次数</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #409eff">
            {{ ((report?.summary?.ac_rate ?? 0) * 100).toFixed(1) }}%
          </div>
          <div class="stat-label">通过率</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #f56c6c">
            {{ report?.summary?.wrong_pending ?? 0 }}
          </div>
          <div class="stat-label">待攻克错题</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value">
            <template v-if="report?.summary?.score_end !== null && report?.summary?.score_end !== undefined">
              {{ report.summary.score_end.toFixed(1) }}
              <span
                class="stat-label"
                :style="{ color: (report.summary.score_delta || 0) >= 0 ? '#67c23a' : '#f56c6c' }"
              >
                {{ (report.summary.score_delta || 0) >= 0 ? '↑' : '↓' }}{{ Math.abs(report.summary.score_delta || 0).toFixed(1) }}
              </span>
            </template>
            <template v-else>-</template>
          </div>
          <div class="stat-label">学习状态变化</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card class="page-card" shadow="never">
          <div class="stat-value" style="color: #e6a23c">
            {{ report?.streak?.current ?? 0 }}
          </div>
          <div class="stat-label">连续打卡（最长 {{ report?.streak?.longest ?? 0 }} 天）</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xs="24" :md="14">
        <el-card class="page-card" shadow="never">
          <template #header>每日趋势</template>
          <div ref="dailyEl" style="height: 280px" />
          <div v-if="!report?.daily?.length" class="empty-hint">暂无数据</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="10">
        <el-card class="page-card" shadow="never">
          <template #header>判定分布</template>
          <div ref="verdictEl" style="height: 280px" />
          <div v-if="!Object.keys(report?.verdicts || {}).length" class="empty-hint">暂无数据</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :xs="24" :md="8">
        <el-card class="page-card" shadow="never">
          <template #header>待补强知识点</template>
          <div v-if="report?.weak_tags?.length">
            <div v-for="w in report.weak_tags" :key="w.tag" class="tag-row">
              <span class="tag-name">{{ w.tag }}</span>
              <el-progress
                :percentage="Math.round(w.score)"
                :stroke-width="10"
                :color="w.score < 40 ? '#F56C6C' : w.score < 60 ? '#E6A23C' : '#67C23A'"
                style="flex: 1"
              />
              <span class="stat-label" style="width: 60px; text-align: right">{{ w.score }}</span>
            </div>
          </div>
          <div v-else class="empty-hint">暂无数据</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="16">
        <el-card class="page-card" shadow="never">
          <template #header>Markdown 报告预览</template>
          <pre class="code-block" style="max-height: 320px; overflow: auto">{{ markdown }}</pre>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.tag-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.tag-name {
  width: 88px;
  font-size: 13px;
  color: #606266;
}
</style>
