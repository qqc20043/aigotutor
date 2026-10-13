# AIgoTutor · 多智能体算法刷题平台

对简历「AIgoTutor 多智能体刷题平台」描述的**完整可运行实现**。Python 3.13 + FastAPI + SQLAlchemy 2.0 + SQLite(WAL) 后端，Vue3 + TypeScript + Vite + Element Plus 前端，**零外部服务依赖**，无需任何 API Key 即可跑通全链路（内置 Mock Provider）。

## 快速开始

```bash
# 1. 安装后端依赖
python -m pip install -r requirements.txt

# 2. 灌入种子数据（12 知识点 + 12 道题 + 演示用户 + 48 条提交记录）
python -m scripts.seed --reset

# 3.（可选）构建前端；不构建也能用，只是根路径返回指引而非页面
cd web && pnpm install && pnpm build && cd ..

# 4. 启动服务
python run.py                     # http://127.0.0.1:8000
# 前端界面: http://127.0.0.1:8000/
# 接口文档: http://127.0.0.1:8000/docs
```

演示账号：`demo` / `demo123456`（**管理员**，可访问管理后台）

```bash
# 5. 验证
python -m pytest                      # 266 项单元 + 集成测试
python -m pytest --cov=app            # 覆盖率（CI 阈值 75%）
python -m ruff check .                # 代码质量（0 告警）
python scripts/judge_selftest.py      # 判题沙箱自测 10/10
python scripts/check_migrations.py    # Alembic 迁移 9/9
python scripts/check_deploy.py        # 部署配置校验
python -m scripts.eval_difficulty     # 难度标注一致性评测

# 端到端（需先启动服务）
python scripts/smoke.py               # 31 项基础 HTTP 冒烟
python scripts/smoke_batch_a.py       # 18 项判题/流式/幂等
python scripts/smoke_batch_c.py       # 31 项认证/限流/运营
python scripts/smoke_admin.py         # 21 项管理后台
python scripts/smoke_web.py           # 31 项前端与静态托管
python scripts/loadtest.py --users 10 --duration 8   # 压测
```

## 前端界面

14 个页面（13 个路由级子页面 + 登录页），`pnpm build` 产物由后端静态托管（无需另起服务）：

| 页面 | 路由 | 关键能力 |
|---|---|---|
| 登录/注册 | `#/login` | 演示账号一键填入；**双 token 自动续期** |
| 概览 | `#/dashboard` | 状态评分、下一题推荐（含理由）、待复习、薄弱点、路线图 |
| 题库 | `#/problems` | 多维筛选、随机抽题、新建题目（自动标注） |
| **做题** | `#/problems/:id` | 代码编辑器、真实判题、测试点明细、Agent 归因、**任务进度**、**token 流式题解** |
| **题面导入** | `#/import` | 粘贴洛谷/CF 题面 → **解析预览** → 一键入库（含自动标注与用例） |
| **掌握度画像** | `#/mastery` | **ECharts 雷达图**、**遗忘曲线**、状态时间序列 |
| **复习队列** | `#/review` | FSRS 保持率、四档评分、30 天预测图 |
| 错题本 | `#/wrong-book` | **易错模式聚类**、归因、重做 |
| **打卡与排行** | `#/checkin` | 连击统计、**8 周热力图**、今日进度、排行榜 |
| **学习报告** | `#/report` | 每日趋势、判定分布、薄弱点、**Markdown 下载** |
| **Agent 控制台** | `#/agent` | 6 个 Agent、payload 编辑、**流式执行（首 token 计时）**、任务中断 |
| 提交记录 | `#/submissions` | 抽屉详情、一键生成结构化题解 |
| **管理后台** | `#/admin` | 全局看板、用户管理、题库体检、数据清理（仅管理员） |
| 设置 | `#/settings` | 用户级模型配置（4 种 provider）、判题后端隔离强度警示、导出数据 |

前端工程：26 个源文件、5500 行；`pnpm build` 含 `vue-tsc` 类型检查，2290 模块 / 10.9s / dist 2737KB；路由级懒加载 + 手动分包（vue / element / echarts 分离）。

```bash
cd web
pnpm dev          # 开发模式，5173 端口，/api 自动代理到 8000
pnpm build        # 类型检查 + 构建到 web/dist
pnpm typecheck    # 只做类型检查
```

> 本机 npm 官方源不可达，`.npmrc` 已固化 npmmirror 镜像。esbuild 的安装脚本必须放行（配置在 `pnpm-workspace.yaml` 的 `allowBuilds`），否则无法构建。

## 核心能力

### 判题沙箱（真实执行代码）

| 能力 | 说明 |
|---|---|
| 双后端 | `subprocess`（本机，跨平台）/ `docker`（容器隔离）；`JUDGE_BACKEND` 切换 |
| 判定 | AC / WA / TLE / MLE / RE / CE / OLE / IE |
| 语言 | Python、C++、Java（运行时缺失时明确报错，不静默失败） |
| 隐藏用例 | `hidden=True` 只参与判题，内容不下发，防止用户打印期望输出骗过评测 |
| 工具链自愈 | 自动规避 javac/java 版本错配；剥离 BOM；解释型语言语法预检（CE 不误判为 RE） |

```bash
python scripts/judge_selftest.py            # 实测 10/10
curl http://127.0.0.1:8000/api/judge        # 查看后端能力与隔离强度
```

### token 级流式（SSE）

前端 Agent 控制台与做题页都用到。事件序列：`open → start → token* → field* → done → closed`。`field` 事件在**字段闭合时立即推送**（增量 JSON 解析），前端可提前渲染而不必等整个 JSON 生成完，页面会显示首 token 延迟。

```bash
curl -N -X POST http://127.0.0.1:8000/api/agent/stream \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent":"solution","payload":{"title":"A+B","statement":"输出 a+b"}}'
```

### 并发与幂等

| 机制 | 行为 |
|---|---|
| 幂等键 | `Idempotency-Key` 请求头；重复请求返回同一条提交记录 |
| 代码去重 | 默认 30s 窗口内同用户同题同代码复用记录，避免重复烧模型额度 |
| 任务并发上限 | 默认 8 条 Agent 链路并发，超出排队而非拒绝（`AIGOTUTOR_MAX_PIPELINES`） |
| 写事务串行 | 全局写锁收敛 SQLite 写并发，实测 20 并发写 0 次 `database is locked` |
| 任务对账 | 启动时把残留 `running` 任务标记为 `interrupted`，消除幽灵任务 |

### schema 自愈

模型加字段后老库不会自动补列（`create_all` 只建表）。启动时自动补缺失的列与表（**只做加法，绝不删列改类型**）—— 破坏性变更留给 Alembic。实测补齐 3 列。

### 认证与安全

| 机制 | 说明 |
|---|---|
| 双 token | access（2h）+ refresh（30d）；类型隔离，refresh 不能当通行证用 |
| 主动失效 | **token 版本号**方案：改密 / 登出全部设备 / 停用账号时 +1，所有旧 token 立即失效，无需黑名单存储 |
| 角色 | `user` / `admin`；首个注册用户自动成为管理员 |
| 限流 | 滑动窗口，按路由分组额度（登录 10/min、Agent 30/min、读接口 120/min） |
| 请求体限制 | 默认 2MB，超限返回 413（在中间件层拦截，不解析后才拒绝） |

### 可观测性

```bash
curl http://127.0.0.1:8000/metrics        # Prometheus 格式
curl http://127.0.0.1:8000/api/stats/global   # JSON 快照
```

- **trace id 用 contextvars 贯穿**：每个响应带 `X-Trace-Id`，日志自动携带，无需逐层传参
- **9 类指标**：HTTP 请求/耗时/在途、Agent 调用/降级/token、判题分布、限流拒绝、运行时长
- **路径归一**：`/api/problems/{id}` 而非真实 id，避免时序基数爆炸
- 空指标也输出 HELP/TYPE —— 否则监控系统在数据产生前会报"未知指标"

### 题面导入

```bash
# 粘贴洛谷/CF 题面即可（支持 HTML 片段）
curl -X POST http://127.0.0.1:8000/api/import/preview \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"text":"P1001 A+B Problem\n\n题目描述\n输出 a+b。\n\n输入格式\n两个整数。\n\n输出格式\n一个整数。"}'
```

自动识别标题、题面、输入输出格式、数据范围、样例与知识点，并交给两阶段标注定难度。**不联网抓取**（避免爬虫风险，且离线可用）。

### 渐进式解题提示（提示强度由掌握度决定）

```bash
curl -X POST "http://127.0.0.1:8000/api/problems/<pid>/hint?language=python" \
  -H "Authorization: Bearer <token>"
```

提示分三档递进，**强度由该题知识点的掌握度算定**（不是模型自由发挥）：

| 掌握度 | 强度 | 给什么 |
|---|---|---|
| < 40 或首次接触 | `pseudocode` | 3 步以内的伪代码级骨架（仍不给完整代码） |
| 40 – 65 | `key_observation` | 点出关键性质 + 需要检查的边界 |
| >= 65 | `direction` | 只给思考方向 |

样本不足时**保守升档**：只做过 1–2 次的题，掌握度数字本身不可信，给弱提示会让新手卡死。

强度是策略、内容是表达 —— 策略归规则层（可复现、可测试），表达归模型。模型若试图改强度，`postprocess` 会强制改回。
提示**永远不给可提交的完整代码**：一旦给出完整解法，这道题的训练价值就归零了。

### 复习队列优先（先做哪几道）

```bash
curl "http://127.0.0.1:8000/api/review/priority?time_budget_minutes=60" \
  -H "Authorization: Bearer <token>"
```

与 FSRS 的分工：**FSRS 决定"何时到期"（记忆科学），本 Agent 决定"到期后先做哪个"（学习策略）**。
两者混在一起会让 FSRS 参数被业务策略污染、无法独立调参。

排序信号：逾期紧迫度 ×3 + 掌握度缺口 ×0.8 + 遗忘次数 ×6，并遵守时间预算（每题按 15 分钟估，
宁可少挑也不给一个做不完的队列）。每道题都带可解释的排序理由。

### 学习记忆（Token 可控的上下文注入）

```bash
curl http://127.0.0.1:8000/api/problems/<pid>/hint -X POST -H "Authorization: Bearer <token>"
# 响应里的 memory_tokens 即本次注入量
```

核心设计：**注入的不是历史，是结论。**

刷了 200 题的用户，原始历史轻松超过 10 万 token；但模型真正需要的从来不是"他做过哪些题"，
而是"他现在哪里弱、什么错误模式反复出现、最近是上升还是下滑"。`core/memory.py` 把历史离线聚合成
一份短清单（薄弱点 / 易错模式 / 趋势），**注入量与会话规模解耦** —— 实测 81 次提交压到约 87 token。

只有决策类 Agent（hint / recommend / review / roadmap）注入记忆；判题、难度标注、题解生成
注入只会增加 token 与干扰，因此默认关闭（`BaseAgent.inject_memory = False`）。

### 代码质量两阶段评分

```bash
curl -X POST http://127.0.0.1:8000/api/agent/run \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent":"quality","payload":{"code":"<你的代码>","verdict":"AC"}}'
```

与难度标注同一套两阶段机制，只是对象从"题面难度"换成"代码质量"：

| 阶段 | 负责 | 内容 |
|---|---|---|
| 第一阶段（规则，确定性问题） | `rules.quality_window` | 嵌套深度、代码行数、标识符长度分布、注释比例、边界判断密度 —— 全部可量化可复现 |
| 第二阶段（LLM，开放性问题） | `agents/quality.py` | 可读性观感、边界是否真的周全、命名是否表达意图 —— 需要语义理解 |

**硬约束**：模型只能在规则基线 ±12 分内修正，越界无条件夹回（`clamp_quality`）。
否则模型可以把 6 层嵌套、全单字母变量的代码评成"优秀"，评分就失去区分度。

### 打卡与学习报告

- **日界按用户本地时区计算**（`User.timezone`）—— 否则东八区凌晨打卡会被算进前一天，连击莫名中断
- 连击 / 历史最长连击 / 8 周热力图
- 学习报告支持 JSON 与 Markdown 下载，含每日趋势、判定分布、掌握度变化、薄弱知识点

### 部署

```bash
# 开发（源码挂载 + 热重载）
docker compose up --build

# 生产（Docker 判题隔离 + Caddy 反代 + 限流）
cp .env.example .env    # 改 AIGOTUTOR_SECRET
docker compose -f docker-compose.prod.yml up -d --build
```

镜像特性：多阶段构建（前端产物从构建阶段拷贝，运行镜像不含 node_modules）、非 root 运行、健康检查、tmpfs 判题目录。

## 简历主张 → 实现对照

| 简历原文 | 实现位置 | 验证方式 |
|---|---|---|
| 后端 27 个接口 / 14 张表 | `app/models.py`（17 表）、`app/api/*.py`（75 接口，超集） | `scripts/check_routes.py` |
| 5 个 Agent：路线规划/题目推荐/解题提示/掌握度评估/复习队列优先 | `app/agents/`：`roadmap` / `recommend` / `hint` / `mastery` / `review` 五个核心 Agent；另有 `judge` / `solution` / `difficulty` / `quality` 四个支撑型 Agent | `test_agent_registry_matches_resume_claim`（断言核心 Agent 精确等于这五项职责） |
| 统一基类 + 结构化输出 + Pydantic 校验 | `app/core/agent.py`、`app/schemas.py` | `tests/test_core.py` |
| **单 Agent 失败自动降级为规则引擎，主链不中断** | `BaseAgent._degrade`、`app/core/rules.py` | `test_agent_degrades_to_rule_engine`、`test_pipeline_survives_when_every_agent_fails` |
| 两阶段难度标注（规则区间 + 模型校准） | `core/rules.py::difficulty_window/clamp_to_window` | `test_two_stage_annotation_never_out_of_window`、`scripts/eval_difficulty.py` |
| **两阶段评分：确定性问题用规则、开放性问题用 LLM 语义修正** | 难度标注（`annotate_problem`）+ 代码质量评分（`score_quality` / `agents/quality.py`）：规则算客观指标给出窗口，模型只能在窗口内修正 | `test_quality_clamps_out_of_window_score`、`test_quality_distinguishes_good_from_bad_code` |
| **知识状态建模：动态调整推荐难度与提示强度** | 提示强度由掌握度算定（`rules.hint_strength_for`）：<40 给步骤骨架、40-65 给关键观察、>=65 只给方向；样本不足时保守升档 | `test_hint_strength_decreases_with_mastery`、`test_hint_agent_forces_strength_from_mastery` |
| 0-100 分五级学习状态 + 置信度 | `core/mastery.py`、`agents/mastery.py` | `test_mastery_overview_and_profile` |
| 掌握度画像 + 遗忘曲线 + 间隔重复 | `core/fsrs.py`（FSRS 简化版） | `test_fsrs_*`（6 项） |
| 错题本 + 易错题聚类 | `services.py::cluster_errors` | `test_wrong_book_clustering` |
| **记忆注入：清单仅保留关键字符，避免全量历史注入导致 Token 暴涨** | `core/memory.py`：把历史离线聚合成短清单（薄弱点/易错模式/趋势），只注入结论；实测 81 次提交压到 ~87 token | `test_memory_token_budget_independent_of_history_size`、`test_memory_block_is_conclusion_not_history` |
| 多用户 + 用户级模型配置 | `models.py::UserModelConfig`、`deps.py` | `test_model_config_masks_api_key` |
| OpenAI / Anthropic / Gemini 三协议兼容 | `core/providers.py` | `test_build_provider_all_protocols`、`test_gemini_schema_narrowing` |
| 长链路后台任务 + 轮询 + 流式 + 中断 | `core/tasks.py`、`api/agent.py` | `test_pipeline_async_task_and_sse`、`test_pipeline_task_cancel` |

## 架构

```
web/                     前端（Vue3 + TS + Vite + Element Plus）
├── src/api/             接口封装（14 组路由）+ SSE 流式客户端 + 类型定义
├── src/views/           14 个页面（13 子页面 + 登录）
├── src/layouts/         主布局（侧边栏 + 顶栏）
├── src/stores/          Pinia（登录态）
├── src/utils/           难度/判定/等级配色与格式化
└── dist/                构建产物（由后端静态托管）

app/                     后端（FastAPI）
├── main.py              FastAPI 入口（启动自检 + 任务对账 + 判题探测 + 前端托管）
├── config.py            环境变量配置（默认 mock，离线可用）
├── db.py                异步引擎 + WAL + 全局写锁（写事务串行提交）
├── schema_sync.py       启动时 schema 自愈（只做加法）
├── models.py            17 张表（SQLAlchemy 2.0 声明式）
├── schemas.py           Agent 结构化输出契约（Literal 锁死枚举）
├── security.py          PBKDF2 口令 + HMAC 双 Token + 版本号失效
├── deps.py              FastAPI 依赖（当前用户 / 管理员 / 模型配置 / 分页）
├── middleware.py        限流（滑动窗口）+ 请求体大小限制
├── observability.py     结构化日志 + trace id + Prometheus 指标
├── importer.py          题面解析（洛谷/CF/AtCoder + HTML + 样例状态机）
├── operations.py        打卡 / 排行榜 / 通知 / 学习报告 / 管理后台逻辑
├── services.py          数据访问与业务聚合层
├── judge_service.py     判题服务：用例管理 + 沙箱判题 + 提交去重（临界区）
├── judge/               判题沙箱
│   ├── base.py          统一契约 + 语言规格 + 工具链探测
│   ├── subprocess_backend.py  本机执行（超时/内存/进程树回收）
│   └── docker_backend.py      容器隔离（网络禁用/能力剥夺/cgroup）
├── core/
│   ├── agent.py         Agent 统一基类 + 流式执行 + 增量 JSON 解析 + 记忆注入
│   ├── providers.py     三协议适配 + Mock Provider（prompt 感知）
│   ├── rules.py         规则引擎（9 个 Agent 的确定性兜底 + 提示强度/质量评分口径）
│   ├── pipeline.py      多 Agent 链路编排 + 成本路由
│   ├── memory.py        学习记忆：历史聚合成短清单（Token 可控的上下文注入）
│   ├── fsrs.py          FSRS 简化版（S/D/R 三参数）
│   ├── mastery.py       掌握度 0-100（时间衰减 + 置信度收缩）
│   ├── cache.py         结果缓存（LRU + TTL，成本控制）
│   └── tasks.py         后台任务 + SSE + 中断 + 并发上限
├── agents/              9 个 Agent（各自声明 schema / prompt / 规则兜底）
│                       核心 5 个：roadmap / recommend / hint / mastery / review
│                       支撑 4 个：judge / solution / difficulty / quality
└── api/                 10 组路由，75 个接口

migrations/               Alembic 迁移（破坏性变更用）
deploy/Caddyfile          生产反代（TLS + SSE 不缓冲 + 安全头）
.github/workflows/ci.yml  CI（7 个 job）
Dockerfile                多阶段构建
docker-compose.yml        开发编排
docker-compose.prod.yml   生产编排（Docker 判题隔离）
```

## 四个关键设计决策

### 1. 降级不是 try/except 兜一下，而是"每个 Agent 两条腿走路"

`BaseAgent.run()` 内部完成"重试 → 校验 → 降级"的全过程，**永远不把异常抛给主链路**：

```python
for attempt in range(1, max_retry + 2):
    try:
        data = self.parse(await provider.complete(...))
        return AgentResult(data=data, degraded=False)
    except (TimeoutError, ValidationError, LLMError, Exception) as e:
        last_kind, last_error = ..., str(e)
return await self._degrade(ctx, ...)  # 转规则引擎，标记 degraded=True
```

`practice_pipeline` 里 5 个 Agent 全部不可用时，链路依然返回完整结果。

### 2. 难度标注的可复现性靠"窗口"保证

规则引擎先用零成本特征算出档位与连续分，扩成 ±1 档窗口；LLM 只能在此窗口选，输出后**无条件夹回**：

```python
DIFFICULTY_BOUNDS = [0.0, 27.5, 49.0, 66.5, 82.5, 100.0]  # 唯一的档位映射口径
lo, hi = difficulty_window(rule_out)  # ±1 档
clamped = clamp_to_window(model_out, (lo, hi))
```

全系统只用 `level_index_of()` 一个函数做"分数 → 档位"映射，规则引擎、窗口夹紧、Mock 输出共用，杜绝口径不一致。

### 3. 无信息的校准层必须"遵从"，不能"扰动"

这是实测踩出来的坑。给 Mock 校准层加随机扰动后，逐题诊断显示：**12 道题里改错 1 道、修正 0 道**，与人工标注的 Kappa 从 0.79 掉到 0.68。

结论：没有额外语义信息时，校准层应当恒等映射。测试 `test_calibration_layer_never_hurts_consistency` 就是这条结论的守护网。

### 4. 写并发在"事务提交层"收敛，而不是包每个 flush

要串行化的不是单次 flush，而是"事务提交"这个真正抢写锁的动作。在 `db._commit_serialized` 加全局写锁后，service 层 33 处写操作自动全部覆盖，一处不漏。配合 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`，实测 20 并发写 0 次 `database is locked`。

> 早期版本曾在 `tasks.py` 写过一个 `serialize_write()`，但从未被调用 —— 属于"看起来有、实际无效"的实现，已删除并在事务层重做。

## 实测指标

| 指标 | 实测值 | 命令 |
|---|---|---|
| 单元 + 集成测试 | **266 项通过 / 7 项按环境跳过** | `python -m pytest` |
| 测试覆盖率 | **76%**（CI 门禁 `--cov-fail-under=75`） | `python -m pytest --cov=app` |
| 代码质量 | **ruff 0 告警**（check + format 双通过） | `python -m ruff check .` |
| 判题沙箱自测 | **10/10**（Python 7 种 verdict + Java 3 种） | `python scripts/judge_selftest.py` |
| 数据库迁移 | **9/9**（空库→head→回滚→head） | `python scripts/check_migrations.py` |
| 部署配置校验 | 全部通过（Dockerfile / compose / Caddy / 环境变量） | `python scripts/check_deploy.py` |
| 基础 HTTP 冒烟 | **31/31** | `python scripts/smoke.py` |
| 批次 A 验收（判题/流式/幂等） | **18/18** | `python scripts/smoke_batch_a.py` |
| 批次 C 验收（认证/限流/运营） | **31/31** | `python scripts/smoke_batch_c.py` |
| 管理后台验收 | **21/21** | `python scripts/smoke_admin.py` |
| 前端验收 | **31/31** | `python scripts/smoke_web.py` |
| 并发压测 | **14/14 断言**（4 个场景：read/write/mixed/pipeline），读 191 req/s、写 125 req/s、读 p95 74ms、0 次 `database is locked` | `python scripts/loadtest.py --users 10 --duration 8` |
| 难度标注 Kappa（vs 人工标注） | **0.788** | `python -m scripts.eval_difficulty` |
| 完全一致率 / 越界率 / 可复现性 | **0.833 / 0 / true** | 同上 |
| 业务接口数 / 数据表 | **75 个接口 / 17 张表** | `python scripts/check_routes.py` |
| 后端规模 | `app/` 51 个文件 / 13040 行；含 tests+scripts+migrations 共 83 文件 / 19941 行 | — |
| 前端规模 | 26 个源文件 / 5500 行 / 14 个页面 | — |
| 前端构建 | 2290 模块 / 10.9s / dist 2737KB | `cd web && pnpm build` |

> 端到端冒烟合计 **132 项**（31+18+31+21+31）。全部命令均在本机 Windows + Python 3.13 上实跑得出，非估算。

## 接入真实模型

```bash
export AIGOTUTOR_PROVIDER=openai
export AIGOTUTOR_MODEL=gpt-4o-mini
export AIGOTUTOR_API_KEY=sk-...
export AIGOTUTOR_BASE_URL=https://api.openai.com/v1     # 兼容网关亦可
python run.py
```

或在前端「设置」页按用户维度配置，支持 `purpose=default|fast|reason`。密钥对外一律脱敏。

## 判题沙箱配置

```bash
# 默认：本机子进程（跨平台，隔离弱，适合开发/内网）
export AIGOTUTOR_JUDGE_BACKEND=subprocess
export AIGOTUTOR_JUDGE_TIMEOUT=10        # 秒
export AIGOTUTOR_JUDGE_MEMORY=256        # MB

# 生产：Docker 容器隔离（网络禁用、能力剥夺、只读根文件系统）
export AIGOTUTOR_JUDGE_BACKEND=docker
export AIGOTUTOR_JUDGE_IMAGE=python:3.13-slim

# 关闭判题（仅做 Agent 归因）
export AIGOTUTOR_JUDGE_BACKEND=none
```

**安全边界**：`subprocess` 后端**不是强安全沙箱** —— 它用超时、内存上限、临时目录隔离与输出截断约束行为，但运行的是本机进程。仅适用于本地开发、内网、可信用户的练习场景。**面向公网必须切换 `docker`**。`GET /api/judge` 与前端「设置」页都会如实返回当前隔离强度（`isolation: weak|strong`）。

### Windows 启动注意（勿用 `uvicorn --reload` 直接启动）

Windows 上只有 `ProactorEventLoop` 支持子进程；uvicorn 在 `reload` 或多 worker 时会选用
`SelectorEventLoop`，**判题沙箱随即全部失败**（`create_subprocess_exec` 抛 `NotImplementedError`）。

`python run.py` 已自动处理（reload 时钉住 Proactor，实测与 WatchFiles 热重载共存）。
若绕过它直接启动，请用以下之一：

```bash
uvicorn app.main:app --port 8000                              # 不加 --reload
uvicorn app.main:app --port 8000 --loop asyncio:ProactorEventLoop   # 需要热重载时
```

判题后端另有一道运行时自检：事件循环不支持子进程时，`/api/judge` 会返回
`available: false` 与可操作的原因说明，而不是让用户等到提交才踩坑。

## 已知边界

| 边界 | 说明 |
|---|---|
| subprocess 隔离弱 | 见上。接口会标注 `isolation=weak`，不制造错误的安全感。 |
| C++ 需自备 g++ | 本机无 g++ 时该语言返回明确错误；装上即自动可用（启动时探测）。 |
| Docker 后端未实测 | 本机无 Docker，代码已实现但未在真实 Docker 上验证。 |
| 多 worker | 写锁与任务表是进程内的；`uvicorn --workers N` 需换 MySQL/Postgres 或 Redis 锁。 |
| 限流为单进程内存态 | 滑动窗口计数在进程内，多 worker 会各自计数，需换 Redis 才能全局生效。 |
| 排行榜按 UTC 日界 | 窗口统计用 UTC 零点切分，跨时区用户的"今日"与个人日界可能不一致，接口已附说明。 |
| 通知无外部推送 | 站内通知 + 打卡提醒在应用内产生，未接邮件/短信/WebPush 通道。 |
| 导入不联网抓取 | 题面需粘贴或上传文本，不做远端 URL 抓取（离线可用 + 规避爬虫风险）。 |
| 无向量检索 | 题库检索用 `LIKE` + 标签；错题聚类用标签组合指纹（对强结构化数据比语义相似度更准且零成本）。 |
| FSRS 为简化版 | 保留 S/D/R 三参数，曲线与调度行为正确，但与 Anki 官方参数不可逐位对齐。 |
| 可选依赖缺失会静默降级 | `psutil` 缺失 → Windows 内存采样失效，MLE 会被判成 TLE；`alembic` 缺失 → 迁移验收失败；`tzdata` 缺失 → 打卡日界报时区错误。三者已写进 `requirements.txt`，并由 `check_deploy.py` 断言钉住，防止有人"精简依赖"时踩坑。 |

## 接口清单（73 个业务接口 / 66 条路由）

> `scripts/check_routes.py` 直接遍历 FastAPI 路由表统计，非手工维护。

```
① 认证与用户(9)   POST /api/auth/register|login|refresh|logout|change-password
                  GET,PATCH /api/auth/me
                  GET,PUT /api/users/me/model-config

② 题库与判题(13)  GET,POST /api/problems        GET /api/problems/{id}|random|search|tags
                  POST /api/problems/{id}/route
                  POST /api/problems/{id}/hint            ← 渐进式提示（强度由掌握度决定）
                  GET,POST,PUT /api/problems/{id}/testcases
                  DELETE /api/problems/{id}/testcases/{case_id}
                  GET /api/judge

③ 提交评测(4)     GET,POST /api/submissions      GET /api/submissions/{id}
                  POST /api/submissions/{id}/solution

④ 推荐路线图(3)   GET /api/recommend/next|explain|roadmap

⑤ 复习与掌握度(11) GET /api/review/queue|due|priority    POST /api/review/mark
                  ↑ priority = 复习队列优先 Agent（按学习收益重排 + 解释）
                  GET /api/mastery/profile|overview|timeline    POST /api/mastery/recalc
                  GET /api/wrong-book            POST /api/wrong-book/cluster
                  POST /api/wrong-book/{problem_id}/mastered

⑥ Agent与任务(11) GET /api/agent                 POST /api/agent/run|pipeline|cache/clear
                  GET,POST /api/agent/stream     GET /api/agent/stats
                  GET /api/tasks                 GET /api/tasks/{id}
                  POST /api/tasks/{id}/cancel|wait

⑦ 题面导入(3)     POST /api/import|import/preview|import/batch

⑧ 数据与运维(6)   POST /api/data/import          GET /api/data/export|export/problems
                  GET /api/stats/global          POST /api/stats/cleanup
                  GET /health

⑨ 运营(8)         POST /api/checkin              GET /api/checkin/status
                  GET /api/leaderboard           GET /api/report|report/markdown
                  GET /api/notifications         POST /api/notifications/read|reminders

⑩ 管理后台(7)     GET /api/admin/overview|users|agents|problems/health
                  PATCH /api/admin/users/{user_id}
                  DELETE /api/admin/problems/{problem_id}
                  POST /api/admin/cleanup
```

`/api/import` 与 `/api/data/import` 分工：前者是题面解析入库（面向做题者），后者是题库 JSON 批量导入导出（面向管理员/备份）。
