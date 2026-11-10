<!-- 学习计划列表页：进行中进度卡 + 按分类分组的计划卡片网格（对标 LeetCode Study Plan）。
     零报名制：进度来自 AC 事实，卡片显示「开始 / 继续 / 已完成」。 -->

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { studyPlanApi } from '@/api'
import type { StudyPlan } from '@/api/types'

const router = useRouter()
const loading = ref(false)
const plans = ref<StudyPlan[]>([])

const THEMES: Record<string, string> = {
  green: 'linear-gradient(135deg, #2e7d57 0%, #17422f 100%)',
  blue: 'linear-gradient(135deg, #1f5f9c 0%, #0f3054 100%)',
  purple: 'linear-gradient(135deg, #6b3fa0 0%, #3a2260 100%)',
  orange: 'linear-gradient(135deg, #b96a1f 0%, #6d3c0e 100%)',
  pink: 'linear-gradient(135deg, #a83279 0%, #5f1c45 100%)',
  teal: 'linear-gradient(135deg, #1e7a78 0%, #0f4443 100%)',
}

const inProgress = computed(() => plans.value.filter((p) => p.in_progress))
const groups = computed(() => {
  const order = ['精选', '面试准备', '基础']
  const byCat = new Map<string, StudyPlan[]>()
  for (const p of plans.value) {
    if (!byCat.has(p.category)) byCat.set(p.category, [])
    byCat.get(p.category)!.push(p)
  }
  return order
    .filter((c) => byCat.has(c))
    .map((c) => ({ category: c, items: byCat.get(c)! }))
})

function themeStyle(p: StudyPlan) {
  return { background: THEMES[p.theme] || THEMES.green }
}

function actionLabel(p: StudyPlan) {
  if (p.completed) return '已完成 ✓'
  if (p.done > 0) return '继续学习'
  return '开始学习'
}

async function load() {
  loading.value = true
  try {
    plans.value = (await studyPlanApi.list()).items
  } finally {
    loading.value = false
  }
}

function open(p: StudyPlan) {
  void router.push(`/study-plan/${p.id}`)
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <!-- 进行中：只显示有进度未完成的计划（LeetCode 的「进行中」区块） -->
    <el-card v-if="inProgress.length" class="page-card" shadow="never">
      <template #header>
        <span style="font-weight: 600">进行中</span>
      </template>
      <div class="progress-grid">
        <div v-for="p in inProgress" :key="p.id" class="progress-card" @click="open(p)">
          <div class="progress-icon" :style="themeStyle(p)">{{ p.title.slice(0, 1) }}</div>
          <div class="progress-body">
            <div class="progress-title">{{ p.title }}</div>
            <el-progress
              :percentage="p.percent"
              :stroke-width="6"
              :show-text="false"
              color="#409eff"
            />
            <div class="progress-meta">
              <span class="stat-label">完成进度</span>
              <span class="progress-count">{{ p.done }} / {{ p.total }}</span>
            </div>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 全部计划：按分类分组的卡片网格 -->
    <el-card v-for="g in groups" :key="g.category" class="page-card" shadow="never">
      <template #header>
        <span style="font-weight: 600">{{ g.category }}</span>
      </template>
      <div class="plan-grid">
        <div v-for="p in g.items" :key="p.id" class="plan-card" :style="themeStyle(p)" @click="open(p)">
          <div class="plan-title">{{ p.title }}</div>
          <div class="plan-subtitle">{{ p.subtitle }}</div>
          <div class="plan-spacer" />
          <div class="plan-progress">
            <el-progress
              :percentage="p.percent"
              :stroke-width="5"
              :show-text="false"
              :color="p.completed ? '#67c23a' : '#dcdfe6'"
            />
            <div class="plan-meta">
              <span class="plan-count">{{ p.done }} / {{ p.total }}</span>
              <span class="plan-action">{{ actionLabel(p) }}</span>
            </div>
          </div>
        </div>
      </div>
    </el-card>

    <el-empty v-if="!loading && !plans.length" description="暂无学习计划（管理员可运行 scripts.seed_study_plans 导入）" />
  </div>
</template>

<style scoped>
.progress-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.progress-card {
  display: flex;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.progress-card:hover {
  border-color: var(--el-color-primary-light-5);
}
.progress-icon {
  width: 44px;
  height: 44px;
  border-radius: 8px;
  color: #fff;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.progress-body {
  flex: 1;
  min-width: 0;
}
.progress-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 6px;
}
.progress-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
}
.progress-count {
  font-size: 13px;
  color: #303133;
}
.plan-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 14px;
}
.plan-card {
  display: flex;
  flex-direction: column;
  min-height: 150px;
  padding: 16px;
  border-radius: 10px;
  color: #fff;
  cursor: pointer;
  transition: transform 0.12s, box-shadow 0.12s;
}
.plan-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18);
}
.plan-title {
  font-size: 15px;
  font-weight: 600;
}
.plan-subtitle {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.6;
  color: rgba(255, 255, 255, 0.72);
}
.plan-spacer {
  flex: 1;
}
.plan-progress {
  margin-top: 10px;
}
.plan-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
}
.plan-count {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.86);
}
.plan-action {
  font-size: 12px;
  color: #fff;
  opacity: 0.92;
}
</style>