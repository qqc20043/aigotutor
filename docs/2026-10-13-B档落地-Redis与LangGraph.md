# 执行记录 2026-10-13 · B 档落地（Redis 真接入 + LangGraph 编排）

> **触发**：用户决定执行 B 档 ——「redis 还是要的，到时候写 vite6 就行」。
> **基线**：A 档完成后 266 测试 / 75 接口 / 9 个 Agent。
> **本轮结果**：Redis 真接入（限流/缓存/分布式锁三项）+ LangGraph 真编排；**281 测试通过 / 132 端到端全绿**。

---

## 零、B 档三项的最终处理

| 项 | 决策 | 处理 |
|---|---|---|
| **Redis** | 用户明确要 | ✅ 真接入：连接层 + 限流 + 结果缓存 + 分布式写锁 |
| **LangGraph** | 评估后认为值得引入 | ✅ 真引入：`core/graph.py` 用 StateGraph 表达编排 |
| **Vite 版本** | 用户明确写 Vite 6 | ✅ 只改简历措辞，README 标注 Vite 6（实际依赖就是 `vite ^6.0.5`） |

---

## 一、Redis 接入（`core/redis_client.py`）

### 核心原则：Redis 是可选增强，不是必需依赖

这条原则决定了全部实现方式。项目定位是"clone 下来就能跑通全链路"，
若 Redis 变成硬依赖，`python run.py` 在没起 Redis 的机器上会直接崩。

| 能力 | 未配置 Redis | 配置 Redis 后 |
|---|---|---|
| 限流计数 | 进程内滑动窗口 | Redis ZSET 跨进程共享（`--workers N` 全局一致） |
| 结果缓存 | 进程内 LRU | Redis 共享（重启不丢，多 worker 命中率更高） |
| 写事务锁 | 进程内 `asyncio.Lock` | 分布式锁（SET NX PX + Lua 校验释放） |

**同一份代码跑两种环境**，不需要为环境差异维护两套实现。

### 三个实现细节（都是踩过或想清楚才这么写的）

**1. 降级而不是报错**

```python
def get_redis():
    if not settings.REDIS_URL:
        return None  # 未配置 -> 静默走进程内
    try:
        client = redis.Redis.from_url(..., protocol=2)
        client.ping()  # 提前暴露"服务器没起来"
        return client
    except Exception as e:
        if not _unavailable_logged:  # 只警告一次，不每次刷日志
            logger.warning("Redis 不可用（%s），已降级为进程内实现", ...)
        return None
```

只建连接对象不算可用 —— 服务器没起来时连接对象照样能构造，直到第一次命令才暴露。
这里用 PING 提前暴露，避免"以为有 Redis，实际每次操作都超时"。

**2. 限流 fail-open（而非 fail-closed）**

Redis 抖动时限流**回落进程内**，而不是直接拒绝请求。理由：限流是保护措施，不是业务逻辑。
把用户全拒掉造成的伤害远大于短时失去保护 —— 后者只是暂时少了一层防护，前者是服务不可用。

**3. 兼容 Redis 3.x（实测踩到）**

微软移植版 Redis 3.0.504 不支持 RESP3 的 `HELLO` 握手：

```
$ python -c "import redis; redis.Redis(port=6379).ping()"
redis.exceptions.ResponseError: unknown command 'HELLO'
```

redis-py 5+ 默认会发 HELLO。修法是连接时显式 `protocol=2`（RESP2）——
Redis 3.0 就理解这个协议，生产用的 Redis 6+ 也完全兼容。

### 分布式写锁（`core/lock.py`）

把 `db._commit_serialized` 的进程内 `asyncio.Lock` 换成 `distributed_lock`：

```python
async def _commit_serialized(session):
    async with distributed_lock("write"):  # Redis 可用则跨进程，否则进程内
        _stats["write_tx"] += 1
        await session.commit()
```

**为什么是"锁 + TTL"而不是单纯 SETNX**：持锁进程崩溃时锁不会自动释放，必须给 TTL。
TTL 又不能太短（长事务会被误判为死锁而放行）—— 取 10 秒：SQLite 写事务是毫秒级，
10 秒足够宽裕；真出现 10 秒的写事务说明代码有问题，让它超时释放反而正确。

**为什么释放锁要校验 token**：直接 DEL 会误删**别人的**锁。场景：A 持锁超时释放后
B 拿到锁，此时 A 的 finally 执行 DEL 把 B 的锁删了，C 就能和 B 同时进入临界区。
用 Lua 脚本保证"校验+删除"原子：

```lua
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
```

**抢不到锁怎么办**：不退化为失败，而是回落本地锁继续执行 + 记一次告警。
写事务本身有 SQLite `busy_timeout` 兜底，不该因为锁竞争让用户请求失败。

---

## 二、LangGraph 编排（`core/graph.py`）

### 链路形状显式化

```
START → judge → mastery ─┬─(有候选)→ recommend ─┬─(有标签)→ roadmap → END
                          │                       └─────────→ END
                          ├─(有标签)→ roadmap ────→ END
                          └─→ END
```

**为什么值得用图**（面试会问）：

1. **依赖关系显式化**：链路形状写在 `add_edge` 里，而不是藏在函数调用顺序里；
2. **条件分支是一等公民**："有候选才推荐"、"有标签才画路线图"是真实的条件逻辑。
   手写实现是一串 `if`，图实现是 `add_conditional_edges` —— 加节点时不必改动控制流代码；
3. **状态合并交给 reducer**。

### 最容易踩的坑：状态 reducer

LangGraph 默认是"后写覆盖"。若不声明 reducer，第二个节点返回的 `degraded`
会覆盖第一个的，**汇总统计静默变少且不报错** —— 极难察觉。

```python
class PipelineState(TypedDict, total=False):
    agents: Annotated[dict[str, Any], operator.or_]  # 各节点贡献 {name: result}
    degraded: Annotated[list[str], operator.add]  # 各节点追加自己降级的名字
    failed: Annotated[list[str], operator.add]
```

回归测试 `test_graph_state_reducer_accumulates_all_nodes` 断言四个节点产出一个都不丢。

### 与降级机制的分层（关键设计）

**降级仍在 Agent 层完成**（`BaseAgent.run` 内部重试→校验→规则兜底），图只负责编排。

这是有意的分层：降级是"单个 Agent 如何自救"，编排是"整体流程如何推进"。
混在一起会让两边都难以独立测试。

图节点里另有一层 `try/except` 不是替代 Agent 降级，而是防止 Agent 层**意外抛出**
（比如 bug）把整张图打断 —— 双保险。

### 迁移方式：签名不变，调用方无感

`practice_pipeline` 保留原签名与返回结构，内部委托给 `run_practice_graph`：

```python
async def practice_pipeline(task, *, user_id, config, ...):
    from .graph import run_practice_graph
    return await run_practice_graph(task, user_id=user_id, ...)
```

返回结构新增 `engine: "langgraph"` 字段（便于确认真的走了图），其余字段完全一致。

---

## 三、验证证据

### 3.1 测试

```
$ python -m pytest
281 passed, 7 skipped          （基线 266 → 本轮 +15 条）

新增 tests/test_infra_b.py（15 条）覆盖：
  Redis 未配置返回 None / 运行态如实上报降级 / 连接串脱敏
  限流回落进程内 / 缓存回落进程内 / 锁回落进程内且仍互斥
  图引擎标识 / 条件分支跳过 recommend / 跳过 roadmap
  状态 reducer 累加（防静默覆盖）/ 进度上报 / 全链路降级 / on_result 回调
```

### 3.2 部署校验（新增 12 项断言）

`check_deploy.py` 新增断言把关键契约钉死，防止后人改坏：

```
[PASS] requirements 含 langgraph（多 Agent 编排依赖（缺失则链路无法启动））
[PASS] requirements 含 redis（限流/缓存/分布式锁的跨进程实现依赖）
[PASS] Redis 未配置时返回 None（优雅降级）
[PASS] Redis 连接用 RESP2（兼容 Redis 3.x）
[PASS] 生产 compose 含 redis 服务
[PASS] 生产 compose 配置了 REDIS_URL
[PASS] 存在 LangGraph 编排模块
[PASS] 编排使用 StateGraph
[PASS] 编排声明了状态 reducer（防静默覆盖）
[PASS] pipeline 委托 LangGraph 执行
```

### 3.3 运行态实测（Redis 启用）

```
$ AIGOTUTOR_REDIS_URL=redis://127.0.0.1:6379/0 python run.py
INFO  aigotutor.redis: Redis 已连接：redis://127.0.0.1:6379/0

$ curl /api/stats/global
cache: {"backend": "redis", "size": 2, ...}          ← 缓存走 Redis
write: {"write_tx": 3, ...}                          ← 写锁走分布式锁

$ POST /api/agent/pipeline → 任务完成
引擎: langgraph                                       ← 编排走 LangGraph
执行 Agent: ['judge', 'mastery', 'recommend']
```

### 3.4 端到端（Redis 模式）

```
smoke           == 结果: 31 通过, 0 失败 ==
smoke_batch_a   == 结果: 18 通过, 0 失败 ==
smoke_batch_c   == 结果: 31 通过, 0 失败 ==
smoke_admin     == 结果: 21 通过, 0 失败 ==
smoke_web       == 结果: 31 通过, 0 失败 ==
                合计 132 项全绿
```

### 3.5 分布式锁互斥实测

```
执行顺序: ['enter-1', 'exit-1', 'enter-2', 'exit-2']
互斥: OK（串行）
```

---

## 四、README 已知边界的更新

两条曾经的"已知边界"现已解决，README 中标注为已支持：

| 原边界 | 现状 |
|---|---|
| ~~多 worker~~：写锁与任务表是进程内的 | **已支持**：配 Redis 后写锁跨进程（SET NX PX + Lua） |
| ~~限流为单进程内存态~~ | **已支持**：配 Redis 后 ZSET 跨窗口共享；抖动时 fail-open 回落 |

新增边界条目：**Redis 3.x 兼容**（需 RESP2，已在连接层处理）。

---

## 五、遗留与注意

1. **任务表（`background_tasks`）仍是进程内为主**：`core/tasks.py` 的内存态任务表
   在多 worker 下各自独立（持久化的 `background_tasks` 表是共享的，但运行态事件流
   只在产生它的进程内）。轮询 `GET /api/tasks/{id}` 若被负载均衡到另一个 worker 会 404。
   **生产多 worker 需要 sticky session 或把任务事件也搬进 Redis** —— 这是下一步可做的事，
   当前未做（单 worker 生产与演示场景不受影响）。

2. **`REDIS_URL` 未进 `.env`（只在 `.env.example`）**：符合项目惯例（.env 不入库），
   部署时从模板复制。

3. **本机 Redis 是 3.0.504**（微软移植版，无密码）。生产部署**必须设密码**并在
   URL 里带上（`redis://:pass@host:6379/0`）—— `redis_client._redact()` 会在日志与
   运行态接口里脱敏，不会泄露。

4. **LangGraph 版本要求**：`langgraph>=0.2`，实测 1.2.9 可用。1.x 的 `StateGraph`
   签名与 0.x 有差异（新增 `context_schema` 等），本项目只用最稳定的
   `add_node/add_edge/add_conditional_edges/compile` 子集，跨版本兼容性较好。
