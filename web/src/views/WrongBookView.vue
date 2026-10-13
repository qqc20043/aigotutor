<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { masteryApi } from '@/api'
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

onMounted(load)
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
