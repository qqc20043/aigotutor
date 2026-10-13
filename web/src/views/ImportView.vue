<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { importApi } from '@/api'
import { errorText } from '@/api/http'
import { difficultyType } from '@/utils/format'

const router = useRouter()

const rawText = ref('')
const source = ref('import')
const autoAnnotate = ref(true)
const customTags = ref<string[]>([])
const testcasesText = ref('')

const previewing = ref(false)
const importing = ref(false)
const preview = ref<Record<string, any> | null>(null)
const lastResult = ref<Record<string, any> | null>(null)

/** 批量模式 */
const batchMode = ref(false)
const batchText = ref('')
const batchResults = ref<Array<Record<string, any>>>([])

const SAMPLE = `P1001 A+B Problem

题目描述
输入两个整数 a, b，输出它们的和。

输入格式
一行两个整数 a, b。

输出格式
一个整数，表示 a+b。

样例 #1
输入 #1
1 2
输出 #1
3

数据范围
|a|, |b| <= 10^9
`

const canSubmit = computed(() => rawText.value.trim().length > 0)

/** 把文本形式的用例（输入与输出用 --- 分隔）转成结构化用例 */
function parseTestcases(text: string): Array<Record<string, unknown>> {
  if (!text.trim()) return []
  const blocks = text.split(/\n\s*={3,}\s*\n/)
  const out: Array<Record<string, unknown>> = []
  for (const block of blocks) {
    const m = block.split(/\n\s*---\s*\n/)
    if (m.length >= 2) {
      out.push({
        input: m[0].trim() + '\n',
        output: m[1].trim() + '\n',
        hidden: true,
      })
    }
  }
  return out
}

async function doPreview() {
  if (!canSubmit.value) return
  previewing.value = true
  try {
    preview.value = await importApi.preview(rawText.value, source.value)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    previewing.value = false
  }
}

async function doImport() {
  if (!canSubmit.value) return
  importing.value = true
  try {
    const result = await importApi.one({
      text: rawText.value,
      source: source.value,
      auto_annotate: autoAnnotate.value,
      tags: customTags.value,
      testcases: parseTestcases(testcasesText.value),
    })
    lastResult.value = result
    ElMessage.success(
      `导入成功：${result.title}（难度 ${result.difficulty}，来源 ${result.difficulty_source}）`,
    )
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    importing.value = false
  }
}

async function doBatch() {
  if (!batchText.value.trim()) return
  importing.value = true
  try {
    // 用 ==== 分隔多道题
    const items = batchText.value
      .split(/\n\s*#{3,}\s*\n|\n\s*={4,}\s*\n/)
      .map((t) => t.trim())
      .filter(Boolean)
      .map((text) => ({ text }))
    if (!items.length) {
      ElMessage.warning('没有解析出题目，请用 ==== 分隔多道题')
      return
    }
    const res = await importApi.batch(items, autoAnnotate.value)
    batchResults.value = res.items || []
    ElMessage.success(`批量导入完成：成功 ${res.imported} 道，失败 ${res.failed} 道`)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    importing.value = false
  }
}

function fillSample() {
  rawText.value = SAMPLE
  preview.value = null
}

function goProblem(id: string) {
  void router.push(`/problems/${id}`)
}
</script>

<template>
  <div>
    <el-card class="page-card" shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px">
          <span>题面导入</span>
          <el-radio-group v-model="batchMode" size="small">
            <el-radio-button :value="false">单题</el-radio-button>
            <el-radio-button :value="true">批量</el-radio-button>
          </el-radio-group>
          <div class="flex-spacer" />
          <el-checkbox v-model="autoAnnotate" size="small">
            两阶段难度标注
          </el-checkbox>
        </div>
      </template>

      <el-alert type="info" :closable="false" style="margin-bottom: 12px">
        <div style="line-height: 1.8">
          支持从<b>洛谷 / Codeforces</b> 复制题面文本（含 HTML 片段）直接粘贴解析，
          自动识别标题、题面、输入输出格式、数据范围、样例与知识点。
          <br />不需要联网抓取 —— 避免爬虫风险，也能离线使用。
        </div>
      </el-alert>

      <template v-if="!batchMode">
        <el-input
          v-model="rawText"
          type="textarea"
          :rows="14"
          placeholder="在此粘贴题面文本或 HTML 片段"
        />
        <div class="toolbar" style="margin-top: 12px">
          <el-button size="small" @click="fillSample">填入示例</el-button>
          <el-button size="small" :loading="previewing" :disabled="!canSubmit" @click="doPreview">
            解析预览（不落库）
          </el-button>
          <el-button
            size="small"
            type="primary"
            :loading="importing"
            :disabled="!canSubmit"
            @click="doImport"
          >
            导入题库
          </el-button>
          <el-button size="small" text @click="rawText = ''; preview = null">清空</el-button>
        </div>

        <el-row :gutter="16" style="margin-top: 12px">
          <el-col :xs="24" :md="12">
            <el-card shadow="never">
              <template #header>可选设置</template>
              <el-form label-width="90px" size="small">
                <el-form-item label="来源">
                  <el-select v-model="source" style="width: 100%">
                    <el-option label="自动识别" value="import" />
                    <el-option label="洛谷" value="luogu" />
                    <el-option label="Codeforces" value="codeforces" />
                    <el-option label="AtCoder" value="atcoder" />
                    <el-option label="自编" value="local" />
                  </el-select>
                </el-form-item>
                <el-form-item label="标签">
                  <el-select
                    v-model="customTags"
                    multiple
                    filterable
                    allow-create
                    default-first-option
                    placeholder="留空则自动识别"
                    style="width: 100%"
                  >
                    <el-option v-for="t in ['模拟', '排序', '贪心', '二分', '动态规划', '图论', '并查集', '线段树', '字符串', '数论', '搜索', '前缀和', '双指针', '构造']" :key="t" :label="t" :value="t" />
                  </el-select>
                </el-form-item>
                <el-form-item label="测试用例">
                  <el-input
                    v-model="testcasesText"
                    type="textarea"
                    :rows="5"
                    placeholder="可选。多组用例用 ==== 分隔，输入与输出用 --- 分隔：&#10;1 2&#10;---&#10;3&#10;====&#10;5 7&#10;---&#10;12"
                  />
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :xs="24" :md="12">
            <el-card shadow="never">
              <template #header>解析结果</template>
              <div v-if="preview" class="preview">
                <el-descriptions :column="1" border size="small">
                  <el-descriptions-item label="标题">
                    {{ preview.parsed?.title || '（未识别）' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="来源">
                    {{ preview.parsed?.source }}
                    <span v-if="preview.parsed?.source_id" class="stat-label">
                      #{{ preview.parsed.source_id }}
                    </span>
                  </el-descriptions-item>
                  <el-descriptions-item label="样例数">
                    {{ preview.preview?.samples_found }}
                  </el-descriptions-item>
                  <el-descriptions-item label="识别标签">
                    <el-tag
                      v-for="t in preview.preview?.tags || []"
                      :key="t"
                      size="small"
                      effect="plain"
                      style="margin-right: 4px"
                    >
                      {{ t }}
                    </el-tag>
                    <span v-if="!(preview.preview?.tags || []).length" class="stat-label">无</span>
                  </el-descriptions-item>
                  <el-descriptions-item label="难度提示">
                    {{ preview.parsed?.difficulty_hint || '（无，将用规则引擎推断）' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="规则特征">
                    <span class="stat-label">
                      题面 {{ preview.parsed?.statement_len }} 字 ·
                      标签 {{ preview.parsed?.tag_count }} 个 ·
                      深度 {{ preview.parsed?.algo_depth }} ·
                      {{ preview.parsed?.constraint_tight ? '需优化' : '范围宽松' }}
                    </span>
                  </el-descriptions-item>
                </el-descriptions>

                <el-divider content-position="left">题面预览</el-divider>
                <div class="prose" style="max-height: 200px; overflow: auto">
                  {{ preview.preview?.statement_preview }}
                </div>

                <el-alert
                  v-if="preview.warnings?.length"
                  type="warning"
                  :closable="false"
                  style="margin-top: 10px"
                >
                  <template #title>解析提示</template>
                  <div v-for="(w, i) in preview.warnings" :key="i">· {{ w }}</div>
                </el-alert>
              </div>
              <div v-else class="empty-hint">点击"解析预览"查看识别结果</div>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <!-- 批量模式 -->
      <template v-else>
        <el-input
          v-model="batchText"
          type="textarea"
          :rows="16"
          placeholder="粘贴多道题面，用一行 ==== 分隔每道题"
        />
        <div class="toolbar" style="margin-top: 12px">
          <el-button type="primary" :loading="importing" @click="doBatch">批量导入</el-button>
          <el-button @click="batchText = ''; batchResults = []">清空</el-button>
          <span class="stat-label">每道题之间用一行 ==== 分隔</span>
        </div>

        <el-table v-if="batchResults.length" :data="batchResults" size="small" style="margin-top: 12px">
          <el-table-column label="标题" min-width="180">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="goProblem(row.id)">
                {{ row.title }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column label="难度" width="130">
            <template #default="{ row }">
              <el-tag size="small" :type="difficultyType(row.difficulty)" effect="plain">
                {{ row.difficulty }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="110">
            <template #default="{ row }">{{ row.difficulty_source }}</template>
          </el-table-column>
          <el-table-column label="标签" min-width="160">
            <template #default="{ row }">
              <el-tag v-for="t in row.tags" :key="t" size="small" effect="plain" style="margin-right: 4px">
                {{ t }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <!-- 导入结果 -->
    <el-card v-if="lastResult" class="page-card" shadow="never">
      <template #header>导入结果</template>
      <el-result
        icon="success"
        :title="lastResult.title"
        :sub-title="`难度 ${lastResult.difficulty}（${lastResult.difficulty_source}）· 识别 ${lastResult.samples} 个样例 · ${lastResult.testcases} 个测试用例`"
      >
        <template #extra>
          <el-button type="primary" @click="goProblem(lastResult.id)">去做题</el-button>
          <el-button @click="rawText = ''; lastResult = null">继续导入</el-button>
        </template>
      </el-result>
    </el-card>
  </div>
</template>

<style scoped>
.preview {
  max-height: 520px;
  overflow: auto;
}
</style>
