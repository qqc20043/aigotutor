<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { agentApi, masteryApi, problemApi, recommendApi, reviewApi } from '@/api'
import type { MasteryOverview, RecommendResult, ReviewItem, RoadmapResult } from '@/api/types'
import { difficultyType, levelColor, levelText, retentionColor } from '@/utils/format'

const router = useRouter()
const loading = ref(true)
const mastery = ref<MasteryOverview | null>(null)
const recommend = ref<RecommendResult | null>(null)
const roadmap = ref<RoadmapResult | null>(null)
const dueList = ref<ReviewItem[]>([])
const profile = ref<Array<{ tag: string; score: number; confidence: number }>>([])
const judgeInfo = ref<Record<string, any> | null>(null)
const stats = ref<Record<string, any> | null>(null)

async function load() {
  loading.value = true
  try {
    const [m, r, rm, due, prof, judge, st] = await Promise.allSettled([
      masteryApi.overview(),
      recommendApi.next(),
      recommendApi.roadmap({ weeks: 6 }),
      reviewApi.due(8),
      masteryApi.profile(),
      problemApi.judgeBackend(),
      agentApi.stats(),
    ])
    if (m.status === 'fulfilled') mastery.value = m.value
    if (r.status === 'fulfilled') recommend.value = r.value
    if (rm.status === 'fulfilled') roadmap.value = rm.value
    if (due.status === 'fulfilled') dueList.value = due.value.due
    if (prof.status === 'fulfilled') profile.value = prof.value
    if (judge.status === 'fulfilled') judgeInfo.value = judge.value as unknown as Record<string, any>
    if (st.status === 'fulfilled') stats.value = st.value
  } finally {
    loading.value = false
  }
}

async function goPractice(problemId: string) {
  if (!problemId) {
    ElMessage.warning('没有可推荐的题目')
    return
  }
  void router.push(`/problems/${problemId}`)
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <!-- 顶部指标 -->
    <el-row :gutter="16">
      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="page-card" shadow="hover">
          <div class="stat-value" :style="{ color: levelColor(mastery?.level || 'L1') }">
            {{ mastery?.score?.toFixed(1) ?? '-' }}
          </div>
          <div class="stat-label">
            学习状态评分
            <el-tag size="small" :style="{ marginLeft: '6px' }" effect="plain">
              {{ mastery?.level }} · {{ levelText(mastery?.level || 'L1') }}
            </el-tag>
          </div>
          <el-progress
            :percentage="Math.round(mastery?.score ?? 0)"
            :stroke-width="6"
            :show-text="false"
            style="margin-top: 10px"
          />
          <div class="stat-label" style="margin-top: 6px">
            置信度 {{ ((mastery?.confidence ?? 0) * 100).toFixed(0) }}%
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="page-card" shadow="hover">
          <div class="stat-value" style="color: #409eff">{{ dueList.length }}</div>
          <div class="stat-label">今日待复习</div>
          <div style="margin-top: 10px">
            <el-button size="small" type="primary" plain @click="router.push('/review')">
              去复习
            </el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="page-card" shadow="hover">
          <div class="stat-value" style="color: #67c23a">
            {{ stats?.db?.total_calls ?? 0 }}
          </div>
          <div class="stat-label">近 7 天 Agent 调用</div>
          <div class="stat-label" style="margin-top: 6px">
            降级率 {{ ((stats?.db?.degraded_rate ?? 0) * 100).toFixed(1) }}% ·
            缓存命中 {{ ((stats?.cache?.hit_rate ?? 0) * 100).toFixed(0) }}%
          </div>
        </el-card>
      </el-col>

      <el-col :xs="24" :sm="12" :md="6">
        <el-card class="page-card" shadow="hover">
          <div class="stat-value" style="color: #e6a23c">
            {{ judgeInfo?.languages?.length ?? 0 }}
          </div>
          <div class="stat-label">可判题语言</div>
          <div style="margin-top: 8px">
            <el-tag
              v-for="lang in judgeInfo?.languages || []"
              :key="lang"
              size="small"
              effect="plain"
              style="margin-right: 4px"
            >
              {{ lang }}
            </el-tag>
            <el-tag v-if="judgeInfo?.isolation === 'weak'" size="small" type="warning" effect="plain">
              隔离：弱
            </el-tag>
            <el-tag v-else-if="judgeInfo" size="small" type="success" effect="plain">
              隔离：强
            </el-tag>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <!-- 推荐 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>下一题推荐</span>
              <el-tag v-if="recommend?.degraded" size="small" type="warning" effect="plain">
                规则引擎兜底
              </el-tag>
            </div>
          </template>

          <div v-if="recommend?.problem_id">
            <div class="rec-title">
              {{ recommend.title || recommend.problem_id }}
              <el-tag size="small" :type="difficultyType(recommend.difficulty)" effect="plain">
                {{ recommend.difficulty }}
              </el-tag>
            </div>
            <div style="margin: 8px 0">
              <el-tag
                v-for="t in recommend.tags"
                :key="t"
                size="small"
                effect="plain"
                style="margin-right: 4px"
              >
                {{ t }}
              </el-tag>
            </div>
            <el-alert type="info" :closable="false" style="margin: 10px 0">
              <div class="rec-reason">{{ recommend.reason }}</div>
            </el-alert>
            <div class="rec-foot">
              <span class="stat-label">预期掌握度提升 {{ recommend.expected_gain }}</span>
              <el-button type="primary" size="small" @click="goPractice(recommend.problem_id)">
                开始做题
              </el-button>
            </div>
          </div>
          <div v-else class="empty-hint">暂无推荐（题库为空或没有新题）</div>
        </el-card>
      </el-col>

      <!-- 待复习 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>最近待复习（FSRS）</span>
              <el-button link type="primary" size="small" @click="router.push('/review')">
                全部
              </el-button>
            </div>
          </template>
          <el-table v-if="dueList.length" :data="dueList" size="small" :show-header="false">
            <el-table-column>
              <template #default="{ row }">
                <div class="due-row">
                  <span class="due-title" @click="goPractice(row.problem_id)">
                    {{ row.title }}
                  </span>
                  <div class="flex-spacer" />
                  <el-tag size="small" effect="plain" :color="retentionColor(row.retrievability)">
                    保持率 {{ (row.retrievability * 100).toFixed(0) }}%
                  </el-tag>
                  <span class="stat-label" style="margin-left: 8px">
                    逾期 {{ row.overdue_days }} 天
                  </span>
                </div>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="empty-hint">今天没有需要复习的题目</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <!-- 薄弱知识点 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>薄弱知识点（分数升序）</template>
          <div v-if="profile.length">
            <div v-for="p in profile.slice(0, 6)" :key="p.tag" class="tag-row">
              <span class="tag-name">{{ p.tag }}</span>
              <el-progress
                :percentage="Math.round(p.score)"
                :stroke-width="10"
                :color="p.score < 40 ? '#F56C6C' : p.score < 60 ? '#E6A23C' : '#67C23A'"
                style="flex: 1"
              />
              <span class="stat-label" style="width: 84px; text-align: right">
                {{ p.score.toFixed(1) }} 分
              </span>
            </div>
          </div>
          <div v-else class="empty-hint">暂无画像数据，先做几道题</div>
        </el-card>
      </el-col>

      <!-- 路线图 -->
      <el-col :xs="24" :md="12">
        <el-card class="page-card" shadow="never">
          <template #header>
            <div class="card-head">
              <span>学习路线图</span>
              <el-tag v-if="roadmap?.degraded" size="small" type="warning" effect="plain">
                规则引擎兜底
              </el-tag>
            </div>
          </template>
          <el-steps
            v-if="roadmap?.stages?.length"
            direction="vertical"
            :active="0"
            style="margin-top: 6px"
          >
            <el-step v-for="s in roadmap.stages" :key="s.name" :title="s.name">
              <template #description>
                <div class="stage-desc">
                  <div>
                    <el-tag
                      v-for="t in s.tags"
                      :key="t"
                      size="small"
                      effect="plain"
                      style="margin-right: 4px"
                    >
                      {{ t }}
                    </el-tag>
                    <span class="stat-label">{{ s.weeks }} 周</span>
                  </div>
                  <div class="stat-label" style="margin-top: 4px">{{ s.goal }}</div>
                </div>
              </template>
            </el-step>
          </el-steps>
          <div v-else class="empty-hint">暂无路线图</div>
          <el-alert
            v-if="roadmap?.rationale"
            type="info"
            :closable="false"
            style="margin-top: 12px"
          >
            <div class="rec-reason">{{ roadmap.rationale }}</div>
          </el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="mastery?.suggestion" class="page-card" shadow="never">
      <template #header>学习建议</template>
      <div class="prose">{{ mastery.suggestion }}</div>
    </el-card>
  </div>
</template>

<style scoped>
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.rec-title {
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.rec-reason {
  line-height: 1.7;
  white-space: pre-wrap;
}

.rec-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}

.due-row {
  display: flex;
  align-items: center;
}

.due-title {
  cursor: pointer;
  color: #409eff;
}

.due-title:hover {
  text-decoration: underline;
}

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

.stage-desc {
  padding-bottom: 6px;
}
</style>
