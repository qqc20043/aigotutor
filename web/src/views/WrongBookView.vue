<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { masteryApi, practiceApi, type LeechItem } from '@/api'
import type { ErrorCluster, WrongBookItem } from '@/api/types'
import { difficultyType, fmtRelative, verdictText, verdictType } from '@/utils/format'

const router = useRouter()
const loading = ref(false)
const items = ref<WrongBookItem[]>([])
const clusters = ref<ErrorCluster[]>([])
const masteredFilter = ref<'all' | 'unmastered' | 'mastered'>('unmastered')
const clustering = ref(false)

const filtered = computed(() => {
  if (masteredFilter.value === 'all') return items.value
  if (masteredFilter.value === 'mastered') return items.value.filter((i) => i.mastered)
  return items.value.filter((i) => !i.mastered)
})

async function load() {
  loading.value = true
  try {
    const params = masteredFilter.value === 'all' ? {} : { mastered: masteredFilter.value === 'mastered' }
    const data = await masteryApi.wrongBook({ ...params, limit: 200 })
    items.value = data.items
    clusters.value = data.clusters
  } finally {
    loading.value = false
  }
}

async function recluster() {
  clustering.value = true
  try {
    const res = await masteryApi.cluster()
    clusters.value = res.clusters
    ElMessage.success(`聚类完成，共 ${res.count} 个错误簇`)
  } finally {
    clustering.value = false
  }
}

async function toggleMastered(item: WrongBookItem) {
  await masteryApi.markMastered(item.problem_id, !item.mastered)
  item.mastered = !item.mastered
  ElMessage.success(item.mastered ? '已标记为掌握' : '已取消掌握标记')
}

function goPractice(problemId: string) {
  void router.push(`/problems/${problemId}`)
}

// ---- 顽固错题雷达（Anki leech 工作流的 AI 化）----
const leechLoading = ref(false)
const leeches = ref<LeechItem[]>([])
const leechThresholds = ref<Record<string, any> | null>(null)
const planFor = ref<string>('')
const plan = ref<{ diagnosis: string; sub_problems: string[]; prerequisite_advice: string; next_action: string; degraded: boolean } | null>(null)

async function loadLeech() {
  leechLoading.value = true
  try {
    const out = await practiceApi.leech()
    leeches.value = out.items.filter((i) => i.is_leech)
    leechThresholds.value = out.thresholds
  } finally {
    leechLoading.value = false
  }
}

async function genPlan(item: LeechItem) {
  planFor.value = item.problem_id
  plan.value = null
  try {
    plan.value = await practiceApi.plan(item.problem_id)
  } catch {
    ElMessage.error('生成干预方案失败')
    planFor.value = ''
  }
}

onMounted(() => {
  void load()
  void loadLeech()
})
</script>

<template>
  <div v-loading="loading">
    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <el-radio-group v-model="masteredFilter" @change="load">
          <el-radio-button value="unmastered">未掌握</el-radio-button>
          <el-radio-button value="mastered">已掌握</el-radio-button>
          <el-radio-button value="all">全部</el-radio-button>
        </el-radio-group>
        <el-tag type="info" effect="plain">共 {{ filtered.length }} 题</el-tag>
        <div class="flex-spacer" />
        <el-button :loading="clustering" @click="recluster">
          <el-icon><Connection /></el-icon> 重新聚类
        </el-button>
      </div>
    </el-card>

    <!-- 错误簇 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        易错模式聚类
        <span class="stat-label" style="margin-left: 8px">
          按知识点组合指纹归簇（对强结构化数据比语义相似度更准且零成本）
        </span>
      </template>

      <el-row v-if="clusters.length" :gutter="12">
        <el-col v-for="c in clusters" :key="c.cluster_key" :xs="24" :md="12" :lg="8">
          <el-card shadow="hover" class="cluster-card">
            <div class="cluster-head">
              <span class="cluster-label">{{ c.label }}</span>
              <el-tag size="small" type="danger" effect="plain">严重度 {{ c.severity }}</el-tag>
            </div>
            <div style="margin: 8px 0">
              <el-tag
                v-for="t in c.tags"
                :key="t"
                size="small"
                effect="plain"
                style="margin-right: 4px"
              >
                {{ t }}
              </el-tag>
            </div>
            <div class="stat-label" style="line-height: 1.7">{{ c.suggestion }}</div>
            <div class="stat-label" style="margin-top: 6px">
              {{ c.size }} 道题 · 点开题目可重新练习
            </div>
          </el-card>
        </el-col>
      </el-row>
      <div v-else class="empty-hint">暂无聚类结果，点击"重新聚类"生成</div>
    </el-card>

    <!-- 顽固错题雷达 -->
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>顽固错题雷达</span>
          <el-tag v-if="leechThresholds" size="small" effect="plain" type="danger">
            阈值：遗忘 ≥{{ leechThresholds.lapses }} 次或连错 ≥{{ leechThresholds.wrong_count }} 次
          </el-tag>
          <div class="flex-spacer" />
          <el-button size="small" :loading="leechLoading" @click="loadLeech">刷新</el-button>
        </div>
      </template>
      <el-alert
        type="warning"
        :closable="false"
        style="margin-bottom: 12px"
        title="反复失败的题继续同强度重做收益有限 —— 先看 AI 干预方案（诊断为什么卡、拆子问题、给先修建议），再按方案回来重做"
      />
      <el-table v-if="leeches.length" :data="leeches" size="small">
        <el-table-column label="题目" min-width="160">
          <template #default="{ row }">
            <span class="title-link" @click="goPractice(row.problem_id)">{{ row.title }}</span>
          </template>
        </el-table-column>
        <el-table-column label="顽固程度" min-width="200">
          <template #default="{ row }">
            <span class="stat-label">{{ row.reason }}</span>
          </template>
        </el-table-column>
        <el-table-column label="最弱掌握度" width="100">
          <template #default="{ row }">
            <el-tag size="small" type="danger" effect="plain">{{ row.weakest_mastery }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              size="small"
              :loading="planFor === row.problem_id"
              @click="genPlan(row)"
            >
              AI 干预方案
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-else class="empty-hint">当前没有顽固错题 —— 保持节奏</div>

      <el-card v-if="plan" shadow="never" style="margin-top: 12px">
        <template #header>
          <div style="display: flex; align-items: center; gap: 8px">
            <span>干预方案</span>
            <el-tag v-if="plan.degraded" size="small" effect="plain" type="info">规则模板</el-tag>
          </div>
        </template>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="诊断">{{ plan.diagnosis }}</el-descriptions-item>
          <el-descriptions-item label="子问题拆解">
            <ol style="margin: 0; padding-left: 18px">
              <li v-for="(sp, i) in plan.sub_problems" :key="i">{{ sp }}</li>
            </ol>
          </el-descriptions-item>
          <el-descriptions-item label="先修建议">{{ plan.prerequisite_advice }}</el-descriptions-item>
          <el-descriptions-item label="下一步">{{ plan.next_action }}</el-descriptions-item>
        </el-descriptions>
      </el-card>
    </el-card>

    <!-- 错题列表 -->
    <el-card class="page-card" shadow="never">
      <template #header>错题明细</template>
      <el-table :data="filtered" size="small" stripe>
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

        <el-table-column label="错误次数" width="100">
          <template #default="{ row }">
            <el-tag size="small" type="danger" effect="plain">{{ row.wrong_count }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column label="最近判定" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="verdictType(row.last_verdict)" effect="plain">
              {{ verdictText(row.last_verdict) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="归因" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.root_cause" size="small" effect="plain">{{ row.root_cause }}</el-tag>
            <span v-else class="stat-label">-</span>
          </template>
        </el-table-column>

        <el-table-column label="最近出错" width="110">
          <template #default="{ row }">
            <span class="stat-label">{{ fmtRelative(row.last_wrong_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="goPractice(row.problem_id)">
              重做
            </el-button>
            <el-button link type="success" size="small" @click="toggleMastered(row)">
              {{ row.mastered ? '取消掌握' : '标记掌握' }}
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-hint">没有错题记录</div>
        </template>
      </el-table>
    </el-card>
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

.cluster-card {
  margin-bottom: 12px;
}

.cluster-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.cluster-label {
  font-weight: 600;
  font-size: 14px;
}
</style>
