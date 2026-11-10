<!-- 学习计划详情：计划头（进度）+ 题目清单（完成状态、难度、标签）+ 继续学习。 -->

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { studyPlanApi } from '@/api'
import type { StudyPlanDetail } from '@/api/types'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const data = ref<StudyPlanDetail | null>(null)

/** 难度 → 标签色（入门绿、普及-蓝、普及/提高-橙、普及+红、省选紫） */
function diffType(d: string): 'success' | 'info' | 'warning' | 'danger' | '' {
  if (d === '入门') return 'success'
  if (d === '普及-' || d === '普及/提高-') return 'info'
  if (d === '普及+') return 'warning'
  if (d === '普及+/提高' || d === '提高+/省选') return 'danger'
  return ''
}

const nextItem = computed(() => data.value?.items.find((i) => !i.solved) || null)

const THEMES: Record<string, string> = {
  green: 'linear-gradient(135deg, #2e7d57 0%, #17422f 100%)',
  blue: 'linear-gradient(135deg, #1f5f9c 0%, #0f3054 100%)',
  purple: 'linear-gradient(135deg, #6b3fa0 0%, #3a2260 100%)',
  orange: 'linear-gradient(135deg, #b96a1f 0%, #6d3c0e 100%)',
  pink: 'linear-gradient(135deg, #a83279 0%, #5f1c45 100%)',
  teal: 'linear-gradient(135deg, #1e7a78 0%, #0f4443 100%)',
}

const headerStyle = computed(() => ({
  background: THEMES[data.value?.plan.theme || 'green'] || THEMES.green,
}))

async function load() {
  const id = String(route.params.id || '')
  if (!id) return
  loading.value = true
  try {
    data.value = await studyPlanApi.detail(id)
  } finally {
    loading.value = false
  }
}

function goSolve(problemId: string) {
  void router.push(`/problems/${problemId}`)
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <!-- 计划头：渐变主题卡 + 进度 -->
    <div v-if="data" class="plan-head" :style="headerStyle">
      <div>
        <div class="plan-head-title">{{ data.plan.title }}</div>
        <div class="plan-head-sub">{{ data.plan.subtitle }}</div>
      </div>
      <div class="plan-head-right">
        <div class="plan-head-count">{{ data.plan.done }} / {{ data.plan.total }}</div>
        <el-button v-if="nextItem" size="small" type="primary" @click="goSolve(nextItem.problem_id)">
          继续学习 →
        </el-button>
        <el-tag v-else-if="data.plan.total" size="small" type="success">已全部完成 🎉</el-tag>
      </div>
      <el-progress
        class="plan-head-bar"
        :percentage="data.plan.percent"
        :stroke-width="6"
        :show-text="false"
        color="#67c23a"
      />
    </div>

    <el-card class="page-card" shadow="never">
      <el-table :data="data?.items || []" size="small" stripe>
        <el-table-column label="#" width="56" prop="order_no" />
        <el-table-column label="题目" min-width="220">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="goSolve(row.problem_id)">
              {{ row.title }}
            </el-button>
            <el-tag v-if="row.tags.length" size="small" effect="plain" style="margin-left: 8px">
              {{ row.tags[0] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="难度" width="130">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="diffType(row.difficulty)">
              {{ row.difficulty }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.solved" size="small" type="success" effect="plain">已完成</el-tag>
            <span v-else class="stat-label">未完成</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="goSolve(row.problem_id)">
              {{ row.solved ? '再做一次' : '去解题' }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">该计划暂无题目</div>
        </template>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.plan-head {
  position: relative;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 20px 22px 28px;
  border-radius: 10px;
  color: #fff;
  margin-bottom: 16px;
}
.plan-head-title {
  font-size: 20px;
  font-weight: 600;
}
.plan-head-sub {
  margin-top: 6px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.75);
  max-width: 720px;
  line-height: 1.6;
}
.plan-head-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
}
.plan-head-count {
  font-size: 18px;
  font-weight: 600;
}
.plan-head-bar {
  position: absolute;
  left: 22px;
  right: 22px;
  bottom: 12px;
}
</style>