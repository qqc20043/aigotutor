<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { submissionApi } from '@/api'
import type { Submission, SubmissionDetail } from '@/api/types'
import { fmtDate, verdictText, verdictType } from '@/utils/format'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const items = ref<Submission[]>([])
const total = ref(0)
const query = reactive({ page: 1, size: 20, verdict: '' })

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<SubmissionDetail | null>(null)
const generating = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await submissionApi.list({
      page: query.page,
      size: query.size,
      verdict: query.verdict || undefined,
    })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function openDetail(id: string) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await submissionApi.detail(id)
  } finally {
    detailLoading.value = false
  }
}

async function generateSolution() {
  const id = detail.value?.submission.id
  if (!id) return
  generating.value = true
  try {
    const res = await submissionApi.solution(id)
    if (res.data) {
      ElMessage.success('题解已生成并落库')
      await openDetail(id)
    } else {
      ElMessage.warning(`生成失败：${res.reason || '模型不可用且无兜底方案'}`)
    }
  } finally {
    generating.value = false
  }
}

function goProblem(problemId: string) {
  void router.push(`/problems/${problemId}`)
}

watch(
  () => route.query.id,
  (id) => {
    if (typeof id === 'string' && id) void openDetail(id)
  },
)

onMounted(() => {
  void load()
  const id = route.query.id
  if (typeof id === 'string' && id) void openDetail(id)
})
</script>

<template>
  <div v-loading="loading">
    <el-card class="page-card" shadow="never">
      <div class="toolbar" style="margin-bottom: 0">
        <el-select v-model="query.verdict" placeholder="全部结果" clearable style="width: 150px" @change="load">
          <el-option v-for="v in ['AC', 'WA', 'TLE', 'MLE', 'RE', 'CE']" :key="v" :label="v" :value="v" />
        </el-select>
        <div class="flex-spacer" />
        <el-button @click="load">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <el-table :data="items" size="small" stripe>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="题目" min-width="160">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="goProblem(row.problem_id)">
              查看题目
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="结果" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="verdictType(row.verdict)" effect="plain">
              {{ row.verdict }} · {{ verdictText(row.verdict) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="用例" width="90">
          <template #default="{ row }">{{ row.passed_cases }}/{{ row.total_cases }}</template>
        </el-table-column>
        <el-table-column prop="language" label="语言" width="90" />
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ row.runtime_ms }}ms</template>
        </el-table-column>
        <el-table-column label="判题来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.judged_by === 'sandbox' ? 'success' : 'info'" effect="plain">
              {{ row.judged_by === 'sandbox' ? '沙箱' : '调用方' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row.id)">详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">暂无提交记录</div>
        </template>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 14px">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </el-card>

    <el-drawer v-model="detailVisible" title="提交详情" size="720px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="结果">
              <el-tag size="small" :type="verdictType(detail.submission.verdict)" effect="plain">
                {{ detail.submission.verdict }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="用例">
              {{ detail.submission.passed_cases }}/{{ detail.submission.total_cases }}
            </el-descriptions-item>
            <el-descriptions-item label="语言">{{ detail.submission.language }}</el-descriptions-item>
            <el-descriptions-item label="耗时">{{ detail.submission.runtime_ms }}ms</el-descriptions-item>
            <el-descriptions-item label="提交时间" :span="2">
              {{ fmtDate(detail.submission.created_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <el-divider content-position="left">代码</el-divider>
          <pre class="code-block">{{ detail.code }}</pre>

          <!-- 判题用例明细 -->
          <template v-if="detail.judge_detail?.cases?.length">
            <el-divider content-position="left">测试点明细</el-divider>
            <el-table :data="detail.judge_detail.cases" size="small">
              <el-table-column prop="case_id" label="用例" width="120" />
              <el-table-column label="结果" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="verdictType(row.verdict)" effect="plain">
                    {{ row.verdict }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="time_ms" label="耗时(ms)" width="90" />
              <el-table-column prop="message" label="说明" min-width="140" />
            </el-table>
          </template>

          <el-alert
            v-if="detail.judge_detail?.compile_output"
            type="error"
            :closable="false"
            style="margin-top: 12px"
          >
            <template #title>编译输出</template>
            <pre class="code-block" style="margin-top: 6px">{{ detail.judge_detail.compile_output }}</pre>
          </el-alert>

          <!-- Agent 评审 -->
          <el-divider content-position="left">Agent 评审</el-divider>
          <div style="margin-bottom: 10px">
            <el-button type="primary" size="small" :loading="generating" @click="generateSolution">
              <el-icon><MagicStick /></el-icon> 生成结构化题解
            </el-button>
          </div>

          <el-collapse v-if="detail.reviews.length">
            <el-collapse-item
              v-for="r in detail.reviews"
              :key="r.id"
              :name="r.id"
            >
              <template #title>
                <el-tag size="small" effect="plain" style="margin-right: 8px">{{ r.kind }}</el-tag>
                <span class="stat-label">{{ fmtDate(r.created_at) }}</span>
                <el-tag v-if="r.degraded" size="small" type="warning" effect="plain" style="margin-left: 8px">
                  降级
                </el-tag>
                <el-tag size="small" effect="plain" style="margin-left: 4px">
                  confidence {{ (r.confidence * 100).toFixed(0) }}%
                </el-tag>
              </template>
              <pre class="code-block">{{ JSON.stringify(r.structured, null, 2) }}</pre>
            </el-collapse-item>
          </el-collapse>
          <div v-else class="empty-hint">暂无 Agent 评审</div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>
