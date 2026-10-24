/**
 * 后端契约类型定义。
 * 与 app/api/schemas.py 的 Pydantic 模型逐字段对齐，改动后端时同步这里。
 */

export type Verdict = 'AC' | 'WA' | 'TLE' | 'MLE' | 'RE' | 'CE' | 'OLE' | 'IE' | 'PENDING'
export type Difficulty = '入门' | '普及-' | '普及/提高-' | '普及+/提高' | '提高+/省选'
export type JudgeMode = 'sandbox' | 'caller' | 'auto'
export type Level = 'L1' | 'L2' | 'L3' | 'L4' | 'L5'

export interface TokenOut {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  refresh_expires_in: number
  user_id: string
  username: string
  role: string
}

export interface UserOut {
  id: string
  username: string
  nickname: string
  level: number
  daily_goal: number
  role: string
  timezone: string
  created_at: string
}

export interface ModelConfig {
  provider: string
  model: string
  base_url: string
  api_key_masked: string
  temperature: number
  max_tokens: number
  purpose: string
  enabled: boolean
  is_default: boolean
}

export interface Problem {
  id: string
  title: string
  difficulty: Difficulty
  difficulty_score: number
  difficulty_source: string
  tags: string[]
  source: string
  time_limit_ms: number
  memory_limit_mb: number
  accepted_count: number
  submit_count: number
}

export interface ProblemDetail extends Problem {
  statement: string
  input_desc: string
  output_desc: string
  samples: Array<Record<string, string>>
  constraints: string
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  size: number
}

export interface Tag {
  id: string
  name: string
  parent_id: string | null
  category: string
  description: string
  prereq_ids: string[]
}

export interface TestCase {
  id: string
  order_no: number
  hidden: boolean
  score: number
  note: string
  input_data: string
  expected_output: string
}

export interface Submission {
  id: string
  problem_id: string
  language: string
  verdict: Verdict
  passed_cases: number
  total_cases: number
  runtime_ms: number
  memory_kb: number
  judged_by: string
  created_at: string
}

export interface JudgeFeedback {
  submission_id: string
  verdict: Verdict
  root_cause: string
  explanation: string
  fix_suggestion: string
  degraded: boolean
  confidence: number
}

export interface SubmitResult {
  submission: Submission
  judge: JudgeFeedback | null
  judge_mode: JudgeMode
  verdict: Verdict
  judge_detail: Record<string, any>
  deduplicated: boolean
  reason?: string
}

export interface Review {
  id: string
  kind: string
  content: string
  structured: Record<string, any>
  confidence: number
  degraded: boolean
  created_at: string
}

export interface SubmissionDetail {
  submission: Submission
  code: string
  judge_detail: Record<string, any>
  reviews: Review[]
}

export interface RecommendResult {
  problem_id: string
  title: string
  difficulty: Difficulty
  tags: string[]
  reason: string
  expected_gain: number
  degraded: boolean
  provider: string
}

export interface RoadmapStage {
  name: string
  tags: string[]
  weeks: number
  goal: string
}

export interface RoadmapResult {
  stages: RoadmapStage[]
  rationale: string
  degraded: boolean
}

export interface MasteryProfileRow {
  tag_id: string
  tag: string
  score: number
  confidence: number
  attempts: number
  ac_count: number
  level: Level
}

export interface MasteryOverview {
  score: number
  level: Level
  confidence: number
  weak_tags: string[]
  strong_tags: string[]
  suggestion: string
  metrics: Record<string, any>
}

export interface ReviewItem {
  problem_id: string
  title: string
  difficulty: Difficulty
  stability: number
  difficulty_fsrs: number
  retrievability: number
  interval_days: number
  reps: number
  lapses: number
  due_at: string | null
  is_due: boolean
  overdue_days: number
}

export interface ForgettingPoint {
  day: number
  retention: number
}

export interface ReviewMarkResult {
  ok: boolean
  schedule: {
    problem_id: string
    rating: number
    stability: number
    difficulty: number
    retrievability: number
    interval_days: number
    due_at: string
    reps: number
    lapses: number
  }
  retention_now: number
  forgetting_curve: ForgettingPoint[]
  next_interval_days: number
}

export interface WrongBookItem {
  problem_id: string
  title: string
  difficulty: Difficulty
  wrong_count: number
  last_verdict: Verdict
  root_cause: string
  mastered: boolean
  note: string
  last_wrong_at: string
}

export interface ErrorCluster {
  cluster_key: string
  label: string
  problem_ids: string[]
  tags: string[]
  size: number
  severity: number
  suggestion: string
}

export interface AgentInfo {
  name: string
  schema: string
  cacheable: boolean
  has_rule_fallback: boolean
}

export interface AgentList {
  core_agents: string[]
  agents: AgentInfo[]
}

export interface AgentRunResult {
  agent: string
  status: 'ok' | 'degraded' | 'failed'
  degraded: boolean
  confidence: number
  reason: string
  provider: string
  model: string
  latency_ms: number
  tokens: number
  cached: boolean
  attempts: number
  data: Record<string, any> | null
}

export interface TaskSnapshot {
  task_id: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted'
  progress: number
  stage: string
  error: string
  result: Record<string, any>
  elapsed_ms: number
}

export interface JudgeBackendInfo {
  backend: string
  available: boolean
  languages: string[]
  isolation: string
  powered_by?: string
  note?: string
  memory_limit?: string
  image?: string
  warnings?: string[]
  reason?: string
  configured?: string
  timeout_s?: number
  memory_mb?: number
}

export interface TimelinePoint {
  score: number
  level: Level
  confidence: number
  source: string
  created_at: string
  metrics: Record<string, any>
}

/** SSE 事件（流式接口） */
export interface StreamEvent {
  event: string
  data: Record<string, any>
}

/** 相似题（举一反三）：一条带可解释理由的推荐 */
export interface SimilarProblem {
  problem_id: string
  title: string
  difficulty: string
  difficulty_score: number
  tags: string[]
  shared_tags: string[]
  score: number
  reason: string
}

/** 对话式辅导：SSE done 事件的载荷 */
export interface ChatDoneData {
  reply: string
  strength: string
  degraded: boolean
}
