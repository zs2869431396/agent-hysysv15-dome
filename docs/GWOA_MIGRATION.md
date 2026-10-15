# 从 GWOA 迁移的智能体设计

来源：`D:\GWOA`（企业级 OA 智能体：LangGraph + FastAPI + Vue，120+ 测试）。

**为什么要参考它**：GWOA 已经解决过一批与本项目同类的问题，而且那些设计是被真实故障
逼出来的——不是凭空想出来的模式。下面逐项说明**迁移什么、为什么、在本项目里落到哪**。

---

## 1. 分层职责：这是最重要的共同点

GWOA 的核心设计是一句话：

> **智能体层只发起和查询；审批动作只在 OA 端由对应岗位人工触发，Agent 工具中不暴露
> approve/reject/pay —— 权限边界在服务端强制。**

本项目的对应关系完全同构：

| GWOA | 本项目 |
|---|---|
| Agent 不暴露 approve/reject/pay | **模型不产生 COM 调用、不改文件路径、不决定反应器类型** |
| 权限边界在服务端强制 | **边界在 `compiler.py` + `precheck.py` 强制** |
| 审批动作由人工在 OA 端触发 | **真正的建模动作由确定性代码执行** |

**迁移的是这个原则本身**：模型只产出意图（`ProcessRequest` / `ModelingPlan`），
任何要变成副作用的东西都必须先过确定性代码。

---

## 2. 四层防幻觉（来自 `oa/parsers.py`）

GWOA 处理金额和日期的分层：

```
① 正则/词表     快、免费、确定
② LLM 语义兜底  模糊表达（"大概五百"、"上周三"）
③ 正则校验      防 LLM 幻觉输出脏数据
④ 调用方确认    草稿卡片，用户点头才提交
```

对应到本项目（**这是 P0-C 的主要参考**）：

| 层 | 本项目怎么做 |
|---|---|
| ① 规则 | 从原文正则抓「数值 + 单位」：`10000kg/h`、`380℃`、`2.5MPa`、`710°C` |
| ② LLM 兜底 | 规则覆盖不了的表达（"符合一个工厂一年正常的处理量"）交给模型 |
| ③ **防幻觉校验** | **模型提取的每个数值都必须在原文中出现**（见第 3 节，这是关键） |
| ④ 用户确认 | 阻塞问题（`blocking_questions`）+ 假设清单 |

**GWOA 的第③层是最值得抄的**：`validate_amount`（正数、两位小数）、`validate_date`
（`re.fullmatch` + 真实日期校验）。**校验独立于 LLM，专门抓模型编的值。**

---

## 3. 关键缺口：原文核对（grounding check）

GWOA 的校验针对的是**格式**（金额是不是正数、日期是否合法）。本项目还需要一层它没有的：

> **模型提取的数值，必须能在用户原文里找到。**

因为化学工程里的数值**格式上完全合法**，但可能整句都是模型编的。例如用户说
"进料流量可以自定"，模型直接填 `100 mol/s`——格式没问题，但这是**它自己选的**，
不是用户说的。

**实现要点**：

```python
def check_grounded(field: str, value, source_text: str, allowed_assumptions: dict):
    """数值必须出现在原文，或来自一条被显式接受的假设。"""
    if field in allowed_assumptions:          # 题目允许自定 → 记为假设，不算编造
        return
    if not appears_in(text=normalize(value), haystack=source_text):
        raise UngroundedValue(field, value)   # 异常，不是 warning
```

**为什么不降级成 warning**：编造的数值会算出**看起来合理但无据可依**的结果，
比直接报错危险得多。

---

## 4. 降级链（来自 `agent/supervisor.py`）

GWOA 的注释直接点明了本项目也遇到的坑：

```python
# 1) json_mode 结构化输出
#    注意：DeepSeek thinking 模式不支持 function_calling 的 tool_choice 强制
# 2) 回退：普通文本 → find("{") / rfind("}") 剥离 markdown → JSON 解析
# 3) 重试 1 次
# 4) 规则兜底（不依赖 LLM）
```

**四条都要迁移**：

| 层 | 本项目实现 |
|---|---|
| ① 结构化输出 | `response_format: json_schema`（本端点实测支持） |
| ② **markdown 剥离** | 模型常把 JSON 包在 ` ```json ` 里——用 `find("{")/rfind("}")` 取中间 |
| ③ 重试 | 必填字段校验后重试，**最多 3 次**（实测 83% 一次成功、100% 三次内完整） |
| ④ **规则兜底** | 模型完全不可用时，**明确报「智能体暂不可用」**，绝不猜 |

**第④层的原则**（GWOA 原话："单点故障不雪崩"）：模型挂了不能让整个系统崩，
但**也不能伪造一个结果**。本项目的正确降级是：**报告能力受限并停止**，
而不是退回"用关键词猜一个反应器"。

---

## 5. 多轮状态隔离（来自 `agent/state.py`）

GWOA 踩过并修好的一个 bug：

```python
def _reset_append(current, update):
    """update 为 None 时清空（新一轮），否则追加。"""
    if update is None:
        return []
    return (current or []) + (update or [])
```

> 用 `operator.add` 会**跨轮次累加**，导致第二轮还带着第一轮的数据、
> 汇总的完整性判断失效（**多轮第二问无回答**）。

**本项目直接面临同样的风险**：重整 710°C 与 600°C 是两个工况，如果结果列表跨工况
累加，就会出现"600°C 的结果里混着 710°C 的数据"。

**迁移做法**：所有累积型字段（`cases` / `results_by_case` / `trace` / `errors`）都用
`_reset_append` 语义的 reducer；**每个工况的结果按 `case_id` 存档，不靠列表顺序对应**。

---

## 6. 主图结构（来自 `agent/graph.py`）

GWOA 的图：

```
START → secrecy_check → (REFUSE→END | PASS→supervisor)
      → supervisor → (追问→chat | 单意图→Worker | 多意图→Send 并行 dispatch)
      → workers → aggregator → END
      Checkpoint: AsyncSqliteSaver(thread_id)
```

本项目的主图（同构，节点换成化工语义）：

```mermaid
flowchart TD
    A[START] --> C[intake 需求抽取<br/>规则→LLM→grounding 校验]
    C --> D{阻塞问题?}
    D -->|有| E[interrupt 追问<br/>零副作用]
    E --> C
    D -->|无| F[select 选型<br/>确定性规则]
    F --> G[compile 编译 spec]
    G --> H[precheck 预检<br/>先于 COM 调用]
    H -->|不过| E
    H -->|通过| I[execute 串行执行各工况<br/>一次一个案例]
    I --> J[verify 独立校验]
    J --> K[aggregator 汇总指标]
    K --> L[explain 中文解释]
    L --> M[END]
    Checkpoint: AsyncSqliteSaver(thread_id)
```

**与 GWOA 的顺序差异（要注意）**：GWOA 的 `secrecy_check` 在**一切模型调用之前**，
因为敏感词可以纯规则判定。本项目的预检需要先有 spec，而 spec 来自模型抽取，
所以**预检只能在抽取之后**。能放在抽取之前的只有不需要理解语义的检查
（例如请求里出现明显越界要求）——本项目暂时没有这类检查，故不设该节点。

**可直接沿用的三个具体做法**：

1. **`interrupt()` 与副作用节点分离**——GWOA 把追问放在 `chat_worker`，
   本项目必须保证**追问时不创建任何 HYSYS 案例**。
2. **`Send` 并行只用于无副作用的读取**——本项目的 HYSYS 调用**必须串行**
   （同一实例只能串行），所以多工况用顺序循环，不用 `Send`。
   这是本项目与 GWOA 的**重要差异**，不能照抄。
3. **`thread_id` 作为会话隔离**——同一个 `thread_id` 恢复，不同工况用不同 `case_id`。

---

## 7. 主动控速（来自 `core/rate_limit.py`）

GWOA 有滑动窗口限流器（用于登录）。本项目需要的方向相反但方法相同：

> 服务端会 `429`（实测并发 10 个以上触发）。**客户端要主动控速，而不是等被拒。**

```python
class SlidingWindow:
    """保证任意 window 秒内不超过 max_calls 次。"""
    def acquire(self) -> None:   # 必要时 sleep
        ...
```

**为什么需要**：本轮调研**两次**因为连续请求导致全部失败（一次 24 个请求全挂），
而单次调用完全正常。把它做成显式组件，而不是靠调用方记得加 `sleep`。

---

## 8. 配置与凭据（来自 `core/llm.py` + `core/config.py`）

GWOA 的做法：

```python
if not settings.llm_api_key or settings.llm_api_key.startswith("sk-xxxx"):
    raise RuntimeError("LLM_API_KEY 未配置：……安全红线：密钥禁止硬编码在源码中")
return ChatDeepSeek(..., api_key=SecretStr(settings.llm_api_key), ...)
```

**三点直接迁移**：

1. **fail-fast**：凭据缺失立刻报错，**没有静默兜底**；
2. **`SecretStr`**：即便被打日志也不会打印出明文；
3. **`.env` 不入库**：`.gitignore` 排除，交付仓库里只有 `.env.example`。

**本项目加一条**：凭据只从**环境变量**读（`TR_BASE` / `TR_KEY` / `TR_MODEL`），
因为 AI 对话记录本身要交付，写进任何文件都会泄露。

---

## 9. 不迁移的部分（避免照搬）

| GWOA 的 | 为什么不迁移 |
|---|---|
| FastAPI + REST + JWT + RBAC | 本项目 MVP 里 Agent、界面、HYSYS 在**同一个远程 Windows 会话**内，不需要 HTTP 服务层 |
| Vue 前端 | 先用 CLI；界面另做（见 PROJECT_PLAN 的 P1-F） |
| `Send` 并行分发 | **HYSYS 必须串行**，这是硬约束 |
| MCP / A2A 工具服务化 | 单机单用户，收益低于复杂度 |
| `langchain_deepseek` + `with_structured_output` | 本端点已实测支持 `json_schema`，标准库 `urllib` 直接调即可，少一个依赖；**但保留它的降级链设计**（第 4 节） |
| OA 单据状态机 | 业务不同；本项目用 `WAITING_INPUT / READY / RUNNING / PASS / PARTIAL / FAILED / UNSUPPORTED` |

---

## 10. 迁移清单（落成待办）

| # | 迁移项 | 落到哪个文件 | 来源 |
|---|---|---|---|
| 1 | 四层防幻觉（规则→LLM→校验→确认） | `reactor_agent/extraction.py` + `normalize.py` | `oa/parsers.py` |
| 2 | **原文核对 grounding check** | `extraction.py` | 本项目新增（GWOA 没有的层） |
| 3 | 降级链（schema→markdown剥离→重试→规则） | `reactor_agent/llm.py` | `agent/supervisor.py` |
| 4 | 主动控速滑动窗口 | `llm.py` | `core/rate_limit.py` |
| 5 | 累积字段的 `_reset_append` reducer | `reactor_agent/state.py` | `agent/state.py` |
| 6 | 追问与副作用节点分离 | `graph.py` | `agent/graph.py` |
| 7 | 按 `case_id` 存结果，不靠列表顺序 | `state.py` | `agent/state.py` 的教训 |
| 8 | fail-fast + SecretStr + `.env` 不入库 | `llm.py` / `.gitignore` | `core/llm.py` |
| 9 | 模型不可用时**明确报错**，不伪造结果 | `llm.py` | `agent/intent_fallback.py` 的反向应用 |
