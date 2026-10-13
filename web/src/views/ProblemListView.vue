<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { problemApi } from '@/api'
import type { Problem, Tag } from '@/api/types'
import { DIFFICULTY_ORDER, difficultyType } from '@/utils/format'

const router = useRouter()

const loading = ref(false)
const items = ref<Problem[]>([])
const tags = ref<Tag[]>([])
const total = ref(0)

const query = reactive({
  page: 1,
  size: 12,
  keyword: '',
  difficulty: '',
  tag_id: '',
})

/** 新建题目对话框 */
const createVisible = ref(false)
const creating = ref(false)
const newProblem = reactive({
  title: '',
  statement: '',
  constraints: '',
  tags: [] as string[],
  auto_annotate: true,
})

async function load() {
  loading.value = true
  try {
    const data = await problemApi.list({
      page: query.page,
      size: query.size,
      keyword: query.keyword || undefined,
      difficulty: query.difficulty || undefined,
      tag_id: query.tag_id || undefined,
    })
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function loadTags() {
  try {
    tags.value = await problemApi.tags()
  } catch {
    tags.value = []
  }
}

function search() {
  query.page = 1
  void load()
}

function reset() {
  query.keyword = ''
  query.difficulty = ''
  query.tag_id = ''
  query.page = 1
  void load()
}

function goPractice(id: string) {
  void router.push(`/problems/${id}`)
}

async function randomOne() {
  try {
    const p = await problemApi.random(query.difficulty || undefined)
    void router.push(`/problems/${p.id}`)
  } catch {
    /* 拦截器已提示 */
  }
}

async function submitCreate() {
  if (!newProblem.title.trim()) {
    ElMessage.warning('请填写题目标题')
    return
  }
  creating.value = true
  try {
    const created = await problemApi.create({
      title: newProblem.title,
      statement: newProblem.statement,
      constraints: newProblem.constraints,
      tags: newProblem.tags,
      auto_annotate: newProblem.auto_annotate,
    })
    ElMessage.success(`已创建，难度标注：${created.difficulty}`)
    createVisible.value = false
    newProblem.title = ''
    newProblem.statement = ''
    newProblem.constraints = ''
    newProblem.tags = []
    search()
  } finally {
    creating.value = false
  }
}

onMounted(() => {
  void load()
  void loadTags()
})
</script>

<template>
  <div>
    <el-card class="page-card" shadow="never">
      <div class="toolbar">
        <el-input
          v-model="query.keyword"
          placeholder="搜索标题或题面"
          clearable
          style="width: 240px"
          @keyup.enter="search"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>

        <el-select v-model="query.difficulty" placeholder="全部难度" clearable style="width: 150px">
          <el-option v-for="d in DIFFICULTY_ORDER" :key="d" :label="d" :value="d" />
        </el-select>

        <el-select
          v-model="query.tag_id"
          placeholder="全部知识点"
          clearable
          filterable
          style="width: 170px"
        >
          <el-option v-for="t in tags" :key="t.id" :label="t.name" :value="t.id" />
        </el-select>

        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">重置</el-button>

        <div class="flex-spacer" />

        <el-button @click="randomOne">
          <el-icon><Refresh /></el-icon> 随机一题
        </el-button>
        <el-button type="success" @click="createVisible = true">
          <el-icon><Plus /></el-icon> 新建题目
        </el-button>
      </div>

      <el-table v-loading="loading" :data="items" stripe @row-click="(row: Problem) => goPractice(row.id)">
        <el-table-column label="#" width="56">
          <template #default="{ $index }">
            {{ (query.page - 1) * query.size + $index + 1 }}
          </template>
        </el-table-column>

        <el-table-column label="题目" min-width="220">
          <template #default="{ row }">
            <span class="title-link">{{ row.title }}</span>
          </template>
        </el-table-column>

        <el-table-column label="难度" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="difficultyType(row.difficulty)" effect="plain">
              {{ row.difficulty }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="难度分" width="90">
          <template #default="{ row }">
            <span class="stat-label">{{ row.difficulty_score.toFixed(0) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="知识点" min-width="200">
          <template #default="{ row }">
            <el-tag
              v-for="t in row.tags"
              :key="t"
              size="small"
              effect="plain"
              style="margin-right: 4px"
            >
              {{ t }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="来源" width="90">
          <template #default="{ row }">
            <span class="stat-label">{{ row.source }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="goPractice(row.id)">
              去做题
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-hint">没有匹配的题目</div>
        </template>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 14px">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.size"
          :total="total"
          :page-sizes="[12, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="search"
        />
      </div>
    </el-card>

    <el-dialog v-model="createVisible" title="新建题目" width="640px">
      <el-form label-width="88px">
        <el-form-item label="标题" required>
          <el-input v-model="newProblem.title" placeholder="例如：区间合并" />
        </el-form-item>
        <el-form-item label="题面">
          <el-input
            v-model="newProblem.statement"
            type="textarea"
            :rows="5"
            placeholder="描述题目要求、输入输出格式"
          />
        </el-form-item>
        <el-form-item label="数据范围">
          <el-input v-model="newProblem.constraints" placeholder="例如：1 <= n <= 10^5" />
        </el-form-item>
        <el-form-item label="知识点">
          <el-select
            v-model="newProblem.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="选择或输入知识点"
            style="width: 100%"
          >
            <el-option v-for="t in tags" :key="t.id" :label="t.name" :value="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="自动标注">
          <el-switch v-model="newProblem.auto_annotate" />
          <span class="stat-label" style="margin-left: 10px">
            开启后用"规则区间 + 模型校准"两阶段标注难度
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.title-link {
  color: #409eff;
  cursor: pointer;
  font-weight: 500;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>
