# 审查说明（给独立审查方）

> 这份文档是写给**审查这个项目的 AI 或人**的。目的是让你把精力花在真正的缺陷上，
> 而不是花在"看起来像 bug、其实是设计意图"的地方，或者因为环境原因得出错误结论。
>
> **先读第 2 节（陷阱）再动手。** 那里列的每一条，都是不做说明就极可能被误报的。

---

## 1. 项目是什么

一个能接收自然语言、自主判断反应器类型并说明理由、在 Aspen HYSYS 中创建并求解、
返回可解释结果的系统。两层：

| 层 | 职责 | 依赖 HYSYS |
|---|---|---|
| `hysys_tools/` | 把严格的 JSON 规格变成真实 HYSYS 案例，并**独立校验**结果 | 真机执行时需要 |
| `reactor_agent/` | 自然语言 → 规格 → 工程解释 | 不需要 |

**核心架构约束**：模型的职责**止于"把自然语言变成事实"**。选型由 `selection.py`
的确定性规则推导，规格由 `compiler.py` 生成；任何模型产物都必须经过确定性 Python
才能变成 COM 调用、文件路径或子进程参数。

**权威文档**（按可信度排序）：

1. `REPORT.md` —— 1–2 页总结，含边界与不足
2. `PROJECT_PLAN.md` —— 进度与计划
3. `TECH_STACK.md` —— 技术选型**及实测依据**
4. `docs/TOOL_REFERENCE.md` —— 工具层接口
5. `docs/GWOA_MIGRATION.md` —— 智能体设计的迁移来源
6. `docs/AGENT_IMPLEMENTATION_PLAN.md` —— **规划时的历史记录，不代表当前状态**（见陷阱 9）

---

## 1.5 一轮外部审查的结果（必读，避免重复报告）

另一位审查者对我们提交的版本做了独立复现，**报告了 10 项缺陷，我逐条复现后确认 9 项属实、1 项部分属实**，已全部修复并配上回归测试。诚实记录在此，因为**其中一条推翻了本文件原先的说法**：

> 本文件早先声称"防幻觉检查会拦截编造值"。审查者构造了一个反例：模型编造
> `12345 kg/h` → 系统正确阻止 → 用户回复 `not a number` → **系统放行并把 12345
> 交给了适配器**。原因是 `ask` 节点在恢复时**无条件清空**整个 ungrounded 列表。
> 已修复：只解除"用户确实作答、且答案能解析"的字段。

已修复清单（每条都有回归测试）：

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 无效作答放行编造值 | `ask` 只解除确认过的字段；未解决/无效的继续阻塞并再次追问；追问轮次有上限 |
| 2 | grounding 只认数值、不认单位 | 升级为 **数值 + 单位 + 物理量**；组成也检查（补数被认定为推导）；反应系数按"原文是否写了方程式"区分对待 |
| 3 | 编译器补充的问题没传回主图 | 在 `plan` **编译之后**统一收集全部未解决问题；支持 `q-coal-definition`；删除"答过一次就不再问" |
| 4 | 成功运行不返回计算数值 | 适配器保留 `results()`（组成/温度/压力/转化率/热负荷/CO 收率口径/校验）；`explain` 输出多工况**对比表** |
| 5 | 自定义入口预设 Conversion | 改为"抽取通用事实 → 选型 → 按选中类型的必填项检查 → 追问 → 编译"；主图与流水线共用 |
| 6 | 动力学信息在抽取时丢失 | 抽取补充速率方程/参数/设备体积/相态；`kinetic_data` 不再恒为 None；需 CSTR/PFR 时**明确返回不支持** |
| 7 | 含固体碳的 Gibbs 被标成 verified | `has_solid_reactant` 传入选型 → 气化正确显示 **experimental**；默认热边界也声明为假设 |
| 8 | 失败处理不完整 | 成功需**同时**满足退出码、结果结构、证据；超时停止后续工况；stdout/stderr 落盘 |
| 9 | 流水线恢复时状态算错 | 已完成的工况计入完成数，恢复时返回 PASS 而非 FAILED |
| 10 | （部分）放行重整流量后 assumptions 为空 | **未能复现**：流量假设确实被记录。属实的那一半是**默认热边界没有声明**，已修 |

**未采纳的一条**：审查者建议的"气化组合应显示 experimental"已采纳，但其描述
"assumptions 仍为空"在我们的复现中不成立——这一点请以代码和测试为准，不要沿用。

**仍然存在的真实局限**（不要当成新发现）：

- **真机上的 Agent 端到端仍未跑过**。本机无 HYSYS。
- **本轮所有"执行"类复现都是离线模拟**：使用注入的假模型响应与假适配器，
  **没有连接过 HYSYS**。冻结工具层的 12 个文件哈希与远程验收记录一致，未被改动。

---

## 2. 陷阱：这些看起来像 bug，但都是设计意图

### 陷阱 1：气化场景被"拒绝执行"，不是失败

题目给的是 `80000 Nm3/h`，但煤是固体、水是液体，**Nm³ 只对气体有意义**，且未说明
标况与所指流股。不同解释会给出完全不同的 CO 收率。

系统因此**拒绝执行并列出待澄清问题**（`WAITING_INPUT`，退出码 3，**0 个 `.hsc` 文件**）。
这是**正确行为**，是输入保护，不是未实现。

**判据**：`WAITING_INPUT` + 未创建任何案例 = 通过。

### 陷阱 2：`experimental` 不是"没做完"

能力状态有三种：`verified` / `experimental` / `unsupported`。

- 甲苯（Conversion 绝热）标为 **`experimental`**，但它**已通过真机验收**，与历史基线
  **逐位相同**。标 `experimental` 是因为它的能力表条目保守，不代表结果不可信。
- **`unsupported` 才是不执行**。

### 陷阱 3：`baseline_expected.json` 不是"硬编码的期望输出"

它来自**历史真机结果**，只用于**容差比对**（判断本次运行是否退化），不参与计算、
不影响结果。工具层每次都是真的驱动 HYSYS 并读回结果。

### 陷阱 4：不要尝试 `--execute`

**开发机上没有 HYSYS、也没有 `pywin32`**，`--execute` 必然失败。这不是缺陷。

本机可验证的范围是**到"规格通过预检"为止**（`--graph` 或默认的 dry run 都能走到）。
真机验证请用 `Run-Agent-On-Workstation.cmd`，且**必须在装 HYSYS 的机器上**。

> 同理，不要为了"让测试通过"去装 `pywin32`：`requirements.txt` 刻意把它注释掉，
> 就是为了避免本地环境**看起来**就绪而其实没有 HYSYS。

### 陷阱 5：`hysys_tools/` 是**冻结**的，改动会让验收失效

整个工具层已通过远程真机验收（`tool-layer-runs/acceptance-20261002-144131-d442afae`），
验收记录里有**远程代码的 SHA256**。任何改动都会让那份验收不再覆盖当前代码。

**如果你认为工具层有问题，请报告，不要直接改。** 报告时说明改哪里、为什么、以及
会导致哪些已验证行为需要重新验收。

同理这些文件**不要移动**（有代码按路径引用它们）：

- `baseline_expected.json`（`hysys_tools/remote_check.py` 按 `ROOT / ...` 引用）
- `tool-layer-runs/`（同上）
- `Run-*.cmd`（用 `%~dp0` 定位，移动到别处会失效）

### 陷阱 6：Windows 专用代码不是可移植性缺陷

`adapters/hysys_cli.py` 用 `msvcrt.locking` 做跨进程锁，`hysys_tools/` 用 `win32com`。
HYSYS 本身就只在 Windows 上跑，这不是遗漏。

### 陷阱 7：控制台中文显示为 `?????` 不是编码 bug

Windows 控制台代码页会破坏 UTF-8。CLI 会尝试切到 UTF-8，失败时用替换字符保证不崩。
**中文全文会写入 `explanation.txt`**，那才是给人看的那份。请检查文件，不要以控制台为准。

### 陷阱 8：向后兼容别名不是死代码

`reactor_agent/graph.py` 里有 `_apply_answers`、`_route_after_plan`、`_question_to_dict`
等私有别名。它们是 `nodes/` 拆分后为**不破坏既有调用方**而保留的，有测试使用。

### 陷阱 9：`docs/AGENT_IMPLEMENTATION_PLAN.md` 是历史记录

它记录的是**规划时**的检查项，其中大部分**已经修复**（该文档第 10 节有逐条对照）。
不要把它当成当前状态的描述，也不要按它去"发现"已经修过的问题。

### 陷阱 10：刻意不做的事，不是遗漏

- **不做 CSTR/PFR**：三个场景都未提供动力学参数，属于**已声明**的能力边界。
- **不做 Equilibrium**：工作站上 Ln(K) 源无法设为 Gibbs（恒为 `FixedK=2`），
  与其悄悄用固定 K，不如拒绝。
- **不合并多个 Conversion 反应**：独立校验无法区分各反应各自的转化率，会把总转化率
  冒充成单反应转化率。
- **不把 `Nm3/h` 换算成质量流量**：见陷阱 1。

---

## 3. 五分钟验证（离线，不需要 HYSYS、不需要网络、不需要凭据）

```bash
conda create -n hysys-agent python=3.12 -y && conda activate hysys-agent
python -m pip install -r requirements.txt

# 全部 356 项离线测试（11 个套件）
scripts\run-all-tests.cmd
```

预期：**11 个套件全部 exit 0，共 356 项**（工具层 144 + Agent 层 195 + 打包器 17）。

单项跑法：

```bash
python -m hysys_tools.selfcheck                    # 110 项
python -m unittest reactor_agent.test_graph        # 23 项
python scripts/test_build_submission.py            # 17 项
```

### 需要凭据的端到端（dry run，不会启动任何模拟）

```bash
# PowerShell
$env:TR_KEY = "<key>"; $env:TR_BASE = "https://tokenrhythm.studio/v1"
python -m reactor_agent --graph --scenario toluene
python -m reactor_agent --graph --scenario smr
python -m reactor_agent --graph --scenario gasification
```

预期：

| 场景 | 预期 | 退出码 |
|---|---|---|
| 甲苯 | `status: READY`，`reactor: conversion` | 0 |
| 重整 | `status: READY`，`cases: case-1, case-2`，并列出**一条假设** | 0 |
| 气化 | `PAUSED for clarification`，**未创建任何案例** | 3 |

**没有凭据也能验证大部分逻辑**：几乎所有测试用**注入的假 HTTP transport**，
不联网（这也是刻意的设计，见 `llm.py` 的 `transport` 参数）。

---

## 4. 值得深挖的方向（按我判断的价值排序）

1. **防幻觉检查是否真的拦得住。**
   这是整个项目最重要的一层。试着构造一个"模型编造数值"的场景，确认它**阻塞执行**
   而不是只打个警告，**并且在用户给出无效回答后仍然阻塞**。
   相关测试：`test_pipeline.py::HallucinationBlocksExecution`、
   `test_graph.py::UnreadableAnswerDoesNotReleaseTheValue`（8 项）、
   `test_extraction.py::Grounding`（单位/物理量判别）。
   **这一层出过两次严重问题**：最初只报警不拦截；修好后又因为 `ask` 无条件清空
   ungrounded 而在无效作答后放行。两条路径都值得再挖。

2. **"提问时零副作用"是否成立。**
   请求信息不足时，系统必须**不创建任何案例、不写任何文件**。相关测试：
   `test_graph.py::PausingToAsk`、`test_pipeline.py::BlockedRunsExecuteNothing`。
   试着在真实模型上验证一次（用 `--text` 给一个缺流量的请求）。

3. **"不静默降级"是否处处成立。**
   工具层拒绝的组合，Agent 层是否也会拒绝？有没有哪条路径会**悄悄换一个反应器**
   而不告诉用户？看 `reactor_agent/schemas.py` 的 `SelectionDecision.was_substituted()`
   与 `nodes/explain.py` 里对它的使用（选型替换必须出现在解释里）。

4. **假设是否被如实声明。**
   系统自己选的数值（重整进料流量、三种二甲苯等分）必须出现在
   `assumptions_we_made` / `explanation.txt` 里。试着确认它们**不会**被当成用户给定。

5. **失败是否被如实报告。**
   `adapters/hysys_cli.py` 在**没有 `result.json`** 时必须自己写失败记录，而不是
   假设成功。相关测试：`test_adapters.py::SuccessAndFailure`。

6. **凭据是否真的不会落盘。**
   `TR_KEY` 只从环境变量读。检查有没有任何路径会把它写进日志、检查点或产物。
   相关测试：`test_llm.py::CredentialHandling`、`test_adapters.py` 的 summary 测试。

---

## 5. 已知局限（**已经写进 `REPORT.md`，不算新发现**）

请不要把这些当作独立发现，除非你能证明文档里的说明**不成立**：

| 局限 | 说明 |
|---|---|
| **真机上的 Agent 端到端未跑过** | 本机无 HYSYS。Agent 层验证到"规格通过预检"为止；适配器/台账/暂停恢复有测试覆盖，但未在真实工作站跑完整链路 |
| **平衡常数可能是外推值** | HYSYS 组分 Gibbs 数据温区上限读回 **426.85 K**，而重整工况需要 **873–983 K**（600/710°C）。工具层会输出该 warning |
| **气化无定量结果** | 依赖用户澄清（见陷阱 1）|
| **题目未给氧气进料** | 1400°C 的维持意味着外部供热；题目未提供氧量，系统按 Gibbs 等温处理并声明了这一点 |
| **固体碳的 Gibbs 支持未充分验证** | 只验证过"能加入该组分"，未验证气化的完整路径 |
| **没有图形界面** | 只有 CLI（`python -m reactor_agent`）|

---

## 6. 什么才算真正的发现

欢迎的（举几个例子）：

- **能构造出"编造的值进入了案例"**的输入 → 防幻觉层有洞。
- **能构造出"提问时仍创建了案例"**的路径 → 副作用隔离有洞。
- **某个被拒绝的组合其实可以安全执行**，或有能力表标错（把能跑的标成不行，
  或把没验证的标成 `verified`）。
- **某个必填字段漏检**（例如改了 schema 后 `REQUIRED_BY_KIND` 没跟上）。
- **`REPORT.md` / `README.md` 里的某个数字与证据不符**。
   请你**逐条核对**：报告里的每个数字都应该能在 `tool-layer-runs/` 或测试输出里找到。
- **假设被当成了用户给定**（没出现在 `assumptions_we_made` 里）。
- **凭据有落盘路径**。

请**谨慎报告**的：

- 把陷阱 1–10 里的任何一条当作缺陷。
- 基于"开发机没有 HYSYS"得出的结论。
- 建议在开发机上装 `pywin32` 或改 `hysys_tools/`。
- 把 `experimental` 等同于"未完成"。

---

## 7. 报告发现时的要求

1. **给出可复现的命令**与**实际输出**，不要只给判断。
2. 区分"**我验证了**"与"**我推断**"。
3. 如果是设计意图问题（你认为某个设计决定是错的），请说明**代价**是什么，
   以及**你会怎么权衡**——这个项目里有若干"选择了保守"的地方是刻意的。
4. 涉及 `hysys_tools/` 的改动建议，请说明**需要重新验收哪些行为**。
