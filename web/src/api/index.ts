/**
 * 后端接口封装：按 8 组路由组织，一处改动全站生效。
 */
import { http } from './http'
import type {
  AgentList,
  AgentRunResult,
  ErrorCluster,
  GamificationSummary,
  JudgeBackendInfo,
  MasteryOverview,
  MasteryProfileRow,
  ModelConfig,
  Page,
  Problem,
  ProblemDetail,
  RecommendResult,
  ReviewItem,
  ReviewMarkResult,
  RoadmapResult,
  SimilarProblem,
  Submission,
  SubmissionDetail,
  SubmitResult,
  Tag,
  TaskSnapshot,
  TestCase,
  TimelinePoint,
  TokenOut,
  UserOut,
  WrongBookItem,
} from './types'

// ------------------------------------------------------------ ① 用户与配置
export const authApi = {
  register: (body: { username: string; password: string; nickname?: string; level?: number }) =>
    http.post<TokenOut>('/auth/register', body).then((r) => r.data),
  login: (body: { username: string; password: string }) =>
    http.post<TokenOut>('/auth/login', body).then((r) => r.data),
  refresh: (refreshToken: string) =>
    http.post<TokenOut>('/auth/refresh', { refresh_token: refreshToken }).then((r) => r.data),
  logout: (allDevices = false) =>
    http.post<Record<string, any>>('/auth/logout', null, { params: { all_devices: allDevices } }).then((r) => r.data),
  changePassword: (oldPassword: string, newPassword: string) =>
    http
      .post<Record<string, any>>('/auth/change-password', {
        old_password: oldPassword,
        new_password: newPassword,
      })
      .then((r) => r.data),
  me: () => http.get<UserOut>('/auth/me').then((r) => r.data),
  updateProfile: (body: {
    nickname?: string
    level?: number
    daily_goal?: number
    timezone?: string
  }) => http.patch<UserOut>('/auth/me', body).then((r) => r.data),
  getModelConfig: (purpose = 'default') =>
    http.get<ModelConfig>('/users/me/model-config', { params: { purpose } }).then((r) => r.data),
  setModelConfig: (body: Record<string, unknown>) =>
    http.put<ModelConfig>('/users/me/model-config', body).then((r) => r.data),
}

// ---------------------------------------------------------------- ② 题库
export const problemApi = {
  list: (params: {
    page?: number
    size?: number
    difficulty?: string
    tag_id?: string
    keyword?: string
    source?: string
  }) => http.get<Page<Problem>>('/problems', { params }).then((r) => r.data),

  detail: (id: string) => http.get<ProblemDetail>(`/problems/${id}`).then((r) => r.data),

  random: (difficulty?: string) =>
    http.get<ProblemDetail>('/problems/random', { params: { difficulty } }).then((r) => r.data),

  search: (q: string, limit = 20) =>
    http.get<{ items: Problem[]; total: number }>('/problems/search', { params: { q, limit } }).then((r) => r.data),

  tags: () => http.get<Tag[]>('/problems/tags').then((r) => r.data),

  create: (body: Record<string, unknown>) =>
    http.post<Problem>('/problems', body).then((r) => r.data),

  route: (id: string) =>
    http.post<Record<string, unknown>>(`/problems/${id}/route`).then((r) => r.data),

  testcases: (id: string) =>
    http.get<{ items: TestCase[]; total: number; hidden_count: number }>(`/problems/${id}/testcases`).then((r) => r.data),

  addTestcase: (id: string, body: Record<string, unknown>) =>
    http.post<TestCase>(`/problems/${id}/testcases`, body).then((r) => r.data),

  batchTestcases: (id: string, cases: Array<Record<string, unknown>>, replace = true) =>
    http.put<{ saved: number; total: number }>(`/problems/${id}/testcases`, { cases, replace }).then((r) => r.data),

  removeTestcase: (id: string, caseId: string) =>
    http.delete(`/problems/${id}/testcases/${caseId}`).then((r) => r.data),

  judgeBackend: () => http.get<JudgeBackendInfo>('/judge').then((r) => r.data),

  /** 渐进式提示：强度由该题知识点掌握度自动决定（服务端算定，模型无权升降档） */
  hint: (id: string, params: { language?: string; current_code?: string } = {}) =>
    http
      .post<AgentRunResult & { memory_tokens?: number }>(`/problems/${id}/hint`, null, { params })
      .then((r) => r.data),

  /** 相似题（举一反三）：FTS5 召回 + 标签/难度混合排序，每条带"为什么相似" */
  similar: (id: string, limit = 5) =>
    http
      .get<{ problem_id: string; items: SimilarProblem[]; retrieval: { rebuilt: boolean; rows: number; candidates: number } }>(
        `/problems/${id}/similar`,
        { params: { limit } },
      )
      .then((r) => r.data),
}

// ------------------------------------------------------------ ③ 提交与评测
export const submissionApi = {
  submit: (
    body: {
      problem_id: string
      code: string
      language?: string
      judge_mode?: string
      verdict?: string
      run_agents?: boolean
      dedupe?: boolean
    },
    idempotencyKey?: string,
  ) =>
    http
      .post<SubmitResult>('/submissions', body, {
        headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {},
      })
      .then((r) => r.data),

  list: (params: { page?: number; size?: number; problem_id?: string; verdict?: string }) =>
    http.get<Page<Submission>>('/submissions', { params }).then((r) => r.data),

  detail: (id: string) => http.get<SubmissionDetail>(`/submissions/${id}`).then((r) => r.data),

  solution: (id: string) =>
    http.post<AgentRunResult>(`/submissions/${id}/solution`).then((r) => r.data),
}

// ------------------------------------------------------------ ④ 推荐与路线图
export const recommendApi = {
  next: (params: { target_score?: number; limit?: number; exclude_recent?: number } = {}) =>
    http.get<RecommendResult>('/recommend/next', { params }).then((r) => r.data),

  explain: () => http.get<Record<string, any>>('/recommend/explain').then((r) => r.data),

  roadmap: (params: { weeks?: number; target_level?: string; daily_hours?: number } = {}) =>
    http.get<RoadmapResult>('/recommend/roadmap', { params }).then((r) => r.data),
}

// ---------------------------------------------------------------- ⑤ 复习
export const reviewApi = {
  queue: (params: { limit?: number; due_only?: boolean } = {}) =>
    http
      .get<{ items: ReviewItem[]; total: number; due_count: number }>('/review/queue', { params })
      .then((r) => r.data),

  due: (limit = 20) =>
    http
      .get<{ due: ReviewItem[]; due_count: number; forecast: Array<{ date: string; count: number }> }>(
        '/review/due',
        { params: { limit } },
      )
      .then((r) => r.data),

  mark: (problem_id: string, rating: number) =>
    http.post<ReviewMarkResult>('/review/mark', { problem_id, rating }).then((r) => r.data),

  /**
   * 复习队列优先 Agent：在 FSRS 已筛出的到期项里决定"先做哪个"并解释原因。
   * 与 queue 的分工：queue 是按到期时间排的客观队列，priority 是按学习收益
   * 重排后的推荐顺序 —— 两者同源，前端可并排展示差异。
   */
  priority: (params: { limit?: number; time_budget_minutes?: number } = {}) =>
    http
      .get<
        AgentRunResult & { due_count: number; message?: string }
      >('/review/priority', { params })
      .then((r) => r.data),
}

// -------------------------------------------------------------- ⑥ 掌握度
export const masteryApi = {
  overview: (windowDays = 30) =>
    http.get<MasteryOverview>('/mastery/overview', { params: { window_days: windowDays } }).then((r) => r.data),

  profile: () => http.get<MasteryProfileRow[]>('/mastery/profile').then((r) => r.data),

  timeline: (limit = 60) =>
    http.get<{ items: TimelinePoint[] }>('/mastery/timeline', { params: { limit } }).then((r) => r.data),

  recalc: (windowDays = 30) =>
    http.post<Record<string, any>>('/mastery/recalc', null, { params: { window_days: windowDays } }).then((r) => r.data),

  wrongBook: (params: { mastered?: boolean; limit?: number } = {}) =>
    http
      .get<{ items: WrongBookItem[]; total: number; clusters: ErrorCluster[]; cluster_count: number }>(
        '/wrong-book',
        { params },
      )
      .then((r) => r.data),

  cluster: () =>
    http.post<{ clusters: ErrorCluster[]; count: number }>('/wrong-book/cluster').then((r) => r.data),

  markMastered: (problemId: string, mastered = true) =>
    http.post(`/wrong-book/${problemId}/mastered`, null, { params: { mastered } }).then((r) => r.data),
}

// ---------------------------------------------------------- ⑦ Agent 与任务
export const agentApi = {
  list: () => http.get<AgentList>('/agent').then((r) => r.data),

  run: (agent: string, payload: Record<string, unknown>, useCache = true) =>
    http.post<AgentRunResult>('/agent/run', { agent, payload, use_cache: useCache }).then((r) => r.data),

  pipeline: (body: {
    submission: Record<string, unknown>
    include_roadmap?: boolean
    weeks?: number
  }) => http.post<TaskSnapshot>('/agent/pipeline', body).then((r) => r.data),

  stats: (days = 7) => http.get<Record<string, any>>('/agent/stats', { params: { days } }).then((r) => r.data),

  clearCache: () => http.post<{ ok: boolean }>('/agent/cache/clear').then((r) => r.data),

  tasks: () =>
    http.get<{ runtime: TaskSnapshot[]; persisted: Array<Record<string, any>> }>('/tasks').then((r) => r.data),

  task: (id: string) => http.get<TaskSnapshot>(`/tasks/${id}`).then((r) => r.data),

  cancelTask: (id: string) => http.post(`/tasks/${id}/cancel`).then((r) => r.data),

  waitTask: (id: string, timeout = 60) =>
    http.post<TaskSnapshot>(`/tasks/${id}/wait`, null, { params: { timeout } }).then((r) => r.data),
}

// ---------------------------------------------------------------- ⑧ 数据
export const dataApi = {
  health: () => http.get('/health').then((r) => r.data),
  globalStats: () => http.get<Record<string, any>>('/stats/global').then((r) => r.data),
  exportUser: () => http.get<Record<string, any>>('/data/export').then((r) => r.data),
  importProblems: (problems: Array<Record<string, unknown>>, autoAnnotate = false) =>
    http.post('/data/import', { problems, auto_annotate: autoAnnotate }).then((r) => r.data),
}

// ------------------------------------------------------- ⑨ 运营（批次 C）
export const importApi = {
  /** 只解析不落库，先看解析结果 */
  preview: (text: string, source = 'import') =>
    http.post<Record<string, any>>('/import/preview', { text, source }).then((r) => r.data),

  /** 导入单题 */
  one: (body: {
    text: string
    source?: string
    auto_annotate?: boolean
    tags?: string[]
    testcases?: Array<Record<string, unknown>>
  }) => http.post<Record<string, any>>('/import', body).then((r) => r.data),

  /** 批量导入 */
  batch: (items: Array<{ text: string; source?: string; tags?: string[] }>, autoAnnotate = true) =>
    http
      .post<Record<string, any>>('/import/batch', { items, auto_annotate: autoAnnotate })
      .then((r) => r.data),
}

export const reportApi = {
  json: (days = 7) => http.get<Record<string, any>>('/report', { params: { days } }).then((r) => r.data),
  /** 返回 Markdown 原文，用于下载 */
  markdown: (days = 7) =>
    http
      .get('/report/markdown', { params: { days }, responseType: 'text' })
      .then((r) => r.data as string),
}

export const checkinApi = {
  do: (note = '') => http.post<Record<string, any>>('/checkin', { note }).then((r) => r.data),
  status: () => http.get<Record<string, any>>('/checkin/status').then((r) => r.data),
}

export const leaderboardApi = {
  get: (params: { metric?: string; window?: string; limit?: number } = {}) =>
    http.get<Record<string, any>>('/leaderboard', { params }).then((r) => r.data),
}

export const notificationApi = {
  list: (params: { unread_only?: boolean; limit?: number } = {}) =>
    http.get<Record<string, any>>('/notifications', { params }).then((r) => r.data),
  markRead: (ids: string[] = []) =>
    http.post<Record<string, any>>('/notifications/read', { ids }).then((r) => r.data),
  triggerReminders: () => http.post<Record<string, any>>('/notifications/reminders').then((r) => r.data),
}

// ------------------------------------------------------- ⑩ 管理后台
export const adminApi = {
  overview: (days = 7) => http.get<Record<string, any>>('/admin/overview', { params: { days } }).then((r) => r.data),
  users: (params: { page?: number; size?: number; keyword?: string } = {}) =>
    http.get<Record<string, any>>('/admin/users', { params }).then((r) => r.data),
  patchUser: (id: string, body: { role?: string; is_active?: boolean; daily_goal?: number }) =>
    http.patch<Record<string, any>>(`/admin/users/${id}`, body).then((r) => r.data),
  problemsHealth: (limit = 50) =>
    http.get<Record<string, any>>('/admin/problems/health', { params: { limit } }).then((r) => r.data),
  deleteProblem: (id: string) =>
    http.delete<Record<string, any>>(`/admin/problems/${id}`, { params: { confirm: true } }).then((r) => r.data),
  cleanup: () => http.post<Record<string, any>>('/admin/cleanup').then((r) => r.data),
  agents: (days = 7) => http.get<Record<string, any>>('/admin/agents', { params: { days } }).then((r) => r.data),

  /** 题目管理（对标 Hydro 内容工作台）：列表含健康信息 */
  adminProblems: (params: { page?: number; size?: number; keyword?: string } = {}) =>
    http
      .get<{
        total: number
        items: Array<{
          id: string
          title: string
          difficulty: string
          difficulty_score: number
          tags: string[]
          case_count: number
          submit_count: number
          accepted_count: number
          created_by: string
          time_limit_ms: number
          memory_limit_mb: number
        }>
      }>('/admin/problems', { params })
      .then((r) => r.data),

  /** 一站式建题：题面 + 用例一起提交，建完即可判题 */
  adminCreateProblem: (body: Record<string, unknown>) =>
    http
      .post<{ id: string; title: string; cases_saved: number; judgeable: boolean }>('/admin/problems', body)
      .then((r) => r.data),

  adminPatchProblem: (id: string, body: Record<string, unknown>) =>
    http.patch<{ ok: boolean; updated: string[] }>(`/admin/problems/${id}`, body).then((r) => r.data),

  /** 全平台提交管理（管理员视角） */
  adminSubmissions: (params: { page?: number; size?: number; username?: string; verdict?: string } = {}) =>
    http
      .get<{
        total: number
        items: Array<{
          id: string
          username: string
          problem_id: string
          problem_title: string
          language: string
          verdict: string
          passed_cases: number
          total_cases: number
          runtime_ms: number
          judged_by: string
          created_at: string
          code_preview: string
        }>
      }>('/admin/submissions', { params })
      .then((r) => r.data),

  /** 管理员重判 */
  adminRejudge: (submissionId: string) =>
    http
      .post<{ ok: boolean; old_verdict: string; new_verdict: string }>(
        `/admin/submissions/${submissionId}/rejudge`,
      )
      .then((r) => r.data),

  /** 单用户档案 */
  adminUserDetail: (userId: string) =>
    http.get<Record<string, any>>(`/admin/users/${userId}`).then((r) => r.data),

  /** 重置用户密码（返回一次性临时密码） */
  adminResetPassword: (userId: string) =>
    http
      .post<{ ok: boolean; username: string; temp_password: string; note: string }>(
        `/admin/users/${userId}/reset-password`,
      )
      .then((r) => r.data),

  /** 审计日志（谁在何时对什么做了什么） */
  adminAudit: (params: { page?: number; size?: number; action?: string; actor?: string } = {}) =>
    http
      .get<{
        total: number
        items: Array<{
          id: string
          actor_name: string
          action: string
          action_label: string
          target_type: string
          target_id: string
          detail: Record<string, any>
          created_at: string
        }>
      }>('/admin/audit', { params })
      .then((r) => r.data),

  /** 批量操作用户 */
  adminBatchUsers: (body: {
    user_ids: string[]
    is_active?: boolean
    role?: string
    daily_goal?: number
  }) =>
    http
      .post<{ ok: boolean; updated: number; skipped: Array<{ user_id: string; reason: string }> }>(
        '/admin/users/batch',
        body,
      )
      .then((r) => r.data),

  /** 查看题目全部用例（含隐藏内容） */
  adminTestcases: (problemId: string) =>
    http
      .get<{
        problem_id: string
        title: string
        items: Array<{ id: string; input: string; output: string; hidden: boolean; score: number }>
      }>(`/admin/problems/${problemId}/testcases`)
      .then((r) => r.data),

  /** 整批替换用例（出题后的日常维护） */
  adminReplaceTestcases: (problemId: string, cases: Array<Record<string, unknown>>) =>
    http
      .put<{ ok: boolean; saved: number; old_count: number }>(
        `/admin/problems/${problemId}/testcases`,
        { cases, replace: true },
      )
      .then((r) => r.data),

  /** 运营趋势：每日提交/AC/新增用户 */
  adminTrends: (days = 30) =>
    http
      .get<{
        days: number
        items: Array<{ date: string; submissions: number; ac: number; new_users: number }>
        totals: { submissions: number; ac: number; new_users: number }
      }>('/admin/trends', { params: { days } })
      .then((r) => r.data),

  /** 判题后端状态 */
  adminJudgeStatus: () =>
    http
      .get<Record<string, any>>('/admin/judge-status')
      .then((r) => r.data),

  /** CSV 批量导入用户（每行：用户名,密码[,昵称]） */
  adminImportUsers: (csv: string) =>
    http
      .post<{
        ok: boolean
        created: Array<{ line: number; username: string }>
        failed: Array<{ line: number; username: string; reason: string }>
        created_count: number
      }>('/admin/users/import', { csv })
      .then((r) => r.data),

  /** 站内公告：username 留空 = 全员广播 */
  adminAnnounce: (body: { title: string; body?: string; link?: string; username?: string }) =>
    http.post<{ ok: boolean; sent: number; scope: string }>('/admin/announce', body).then((r) => r.data),
}

// ------------------------------------------------------------ ⑪ 对话式辅导
export interface ChatMessageOut {
  id: string
  role: 'user' | 'assistant'
  content: string
  strength: string
  degraded: boolean
  created_at: string
}

export interface ChatSessionOut {
  id: string
  problem_id: string
  title: string
  strength: string
  message_count: number
  created_at: string
  updated_at: string
}

export const chatApi = {
  createSession: (problem_id = '') =>
    http.post<ChatSessionOut>('/chat/sessions', { problem_id }).then((r) => r.data),

  sessions: (limit = 20) =>
    http.get<{ items: ChatSessionOut[] }>('/chat/sessions', { params: { limit } }).then((r) => r.data),

  detail: (id: string) =>
    http.get<{ session: ChatSessionOut; messages: ChatMessageOut[] }>(`/chat/sessions/${id}`).then((r) => r.data),

  remove: (id: string) => http.delete(`/chat/sessions/${id}`).then((r) => r.data),

  /** 非流式发送（调试/兜底用；正常走 streamChat） */
  send: (id: string, content: string) =>
    http
      .post<{ message: ChatMessageOut; strength: string; degraded: boolean }>(
        `/chat/sessions/${id}/messages`,
        { content },
      )
      .then((r) => r.data),
}

// ------------------------------------------------------------ ⑫ 模拟面试
export interface InterviewSessionOut {
  id: string
  problem_id: string
  title: string
  kind: string
  time_limit_minutes: number
  message_count: number
  status: string
  hint_requests: number
  submission_ids: string[]
  elapsed_seconds: number
  created_at: string
}

export interface InterviewReportOut {
  verdict: string
  scores: Record<string, any>
  narrative: string
  degraded: boolean
  created_at: string
}

export const interviewApi = {
  start: (problem_id: string, time_limit_minutes = 30) =>
    http
      .post<InterviewSessionOut>('/interview/start', { problem_id, time_limit_minutes })
      .then((r) => r.data),

  list: (limit = 20) =>
    http.get<{ items: InterviewSessionOut[] }>('/interview', { params: { limit } }).then((r) => r.data),

  detail: (id: string) =>
    http
      .get<{
        session: InterviewSessionOut
        messages: Array<{ role: string; content: string; strength: string; degraded: boolean; created_at: string }>
      }>(`/interview/${id}`)
      .then((r) => r.data),

  message: (id: string, content: string, ask_hint = false) =>
    http
      .post<{
        reply: string
        degraded: boolean
        strength: string
        hint_requests: number
        elapsed_seconds: number
        time_limit_minutes: number
      }>(`/interview/${id}/message`, { content, ask_hint })
      .then((r) => r.data),

  submit: (id: string, language: string, code: string) =>
    http
      .post<{ submission_id: string; verdict: string; passed: number; total: number; runtime_ms: number }>(
        `/interview/${id}/submit`,
        { language, code },
      )
      .then((r) => r.data),

  finish: (id: string) =>
    http
      .post<{ ok: boolean; already?: boolean; report_id?: string; scores?: Record<string, any>; narrative?: string; degraded?: boolean }>(
        `/interview/${id}/finish`,
      )
      .then((r) => r.data),

  report: (id: string) =>
    http
      .get<{
        report: InterviewReportOut | null
        session: InterviewSessionOut
        timeline: Array<{ role: string; content: string; strength: string; created_at: string }>
      }>(`/interview/${id}/report`)
      .then((r) => r.data),
}

// ------------------------------------------------------------ ⑭ 成长体系
export const gamificationApi = {
  summary: () => http.get<GamificationSummary>('/gamification').then((r) => r.data),
}

// ------------------------------------------------------------ ⑬ 智能练习
export interface LeechItem {
  problem_id: string
  title: string
  difficulty: string
  tags: string[]
  lapses: number
  wrong_count: number
  weakest_mastery: number
  is_leech: boolean
  suspended: boolean
  reason: string
  urgency: number
}

export interface PracticeItem {
  problem_id: string
  title: string
  difficulty: string
  source: 'review' | 'leech' | 'fresh'
  reason: string
  order: number
  primary_tag: string
  tags: string[]
}

export interface PracticeRecipe {
  id: string
  name: string
  recipe: { size: number; include_review: boolean; include_leech: boolean; include_fresh: boolean; tags: string[] }
  created_at: string
}

export const practiceApi = {
  leech: (limit = 20) =>
    http
      .get<{ items: LeechItem[]; thresholds: Record<string, any>; note?: string }>(
        '/practice/leech',
        { params: { limit } },
      )
      .then((r) => r.data),

  interleaved: (body: {
    size?: number
    include_review?: boolean
    include_leech?: boolean
    include_fresh?: boolean
  }) =>
    http
      .post<{
        items: PracticeItem[]
        stats: { size: number; interleaved_pairs: number; sources: Record<string, number> }
        note?: string
      }>('/practice/interleaved', body)
      .then((r) => r.data),

  plan: (problemId: string) =>
    http
      .post<{
        diagnosis: string
        sub_problems: string[]
        prerequisite_advice: string
        next_action: string
        degraded: boolean
      }>(`/practice/leech/${problemId}/plan`)
      .then((r) => r.data),

  /** 搁置/恢复顽固错题（Anki leech：搁置卡移出每日队列，雷达仍可见） */
  suspend: (problemId: string, suspended: boolean) =>
    http.post<{ ok: boolean; suspended: boolean }>(`/practice/leech/${problemId}/suspend`, { suspended }).then((r) => r.data),

  recipes: () => http.get<{ items: PracticeRecipe[] }>('/practice/recipes').then((r) => r.data),

  createRecipe: (body: {
    name: string
    size: number
    tags: string[]
    include_review?: boolean
    include_leech?: boolean
    include_fresh?: boolean
  }) => http.post<PracticeRecipe>('/practice/recipes', body).then((r) => r.data),

  deleteRecipe: (id: string) => http.delete(`/practice/recipes/${id}`).then((r) => r.data),

  /** 按配方生成（动态语义：按当前到期/顽固/新题状态重抽） */
  generateFrom: (id: string) =>
    http
      .post<{
        items: PracticeItem[]
        stats: { size: number; interleaved_pairs: number; sources: Record<string, number> }
        recipe?: { name: string }
        note?: string
      }>(`/practice/recipes/${id}/generate`)
      .then((r) => r.data),
}


