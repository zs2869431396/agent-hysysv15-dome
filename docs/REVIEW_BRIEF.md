# 审查说明（给独立审查方）

> 这份文档是写给**审查这个项目的 AI 或人**的。目的是让你把精力花在真正的缺陷上，
> 而不是花在"看起来像 bug、其实是设计意图"的地方，或者因为环境原因得出错误结论。
>
> **先读第 2 节（陷阱）再动手。** 那里列的每一条，都是不做说明就极可能被误报的。
>
> 本文更新于 2026-10-03 晚，对应"智能体层同步已验收工具层"之后的代码，改动记录见
> `docs/AGENT_FIX_NOTES.md`。早先版本里"气化被拒绝是正确行为""工作站上不能用 Equilibrium"
> 等说法已经失效，请以本文为准。

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
5. `docs/EQUILIBRIUM_INTEGRATION.md` —— Equilibrium 与饱和碳气化的接入说明及验收结论
6. `docs/tool-acceptance-20261003-105341.json` —— 工具层验收清单：版本、验收编号、13 个运行文件的 SHA256
7. `docs/AGENT_FIX_NOTES.md` —— 智能体同步工具层的修改记录
8. `docs/GWOA_MIGRATION.md` —— 智能体设计的迁移来源
9. `docs/AGENT_IMPLEMENTATION_PLAN.md` —— **规划时的历史记录，不代表当前状态**（见陷阱 9）

---

## 1.5 外部审查与后续修复（必读，避免重复报告）

### 第一轮外部审查（2026-10-03 上午）

另一位审查者对当时的版本做了独立复现，**报告了 10 项缺陷，逐条复现后确认 9 项属实、1 项部分属实**，已全部修复并配上回归测试。审查证据冻结在 `docs/review-20261003/`。其中一条推翻了本文件更早的说法：

> 本文件早先声称"防幻觉检查会拦截编造值"。审查者构造了一个反例：模型编造
> `12345 kg/h` → 系统正确阻止 → 用户回复 `not a number` → **系统放行并把 12345
> 交给了适配器**。原因是 `ask` 节点在恢复时**无条件清空**整个 ungrounded 列表。
> 已修复：只解除"用户确实作答、且答案能解析"的字段。

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 无效作答放行编造值 | `ask` 只解除确认过的字段；未解决/无效的继续阻塞并再次追问；追问轮次有上限 |
| 2 | grounding 只认数值、不认单位 | 升级为 **数值 + 单位 + 物理量**；组成也检查（补数被认定为推导）；反应系数按"原文是否写了方程式"区分对待 |
| 3 | 编译器补充的问题没传回主图 | 在 `plan` **编译之后**统一收集全部未解决问题；支持 `q-coal-definition`；删除"答过一次就不再问" |
| 4 | 成功运行不返回计算数值 | 适配器保留 `results()`（组成/温度/压力/转化率/热负荷/CO 收率口径/校验）；输出多工况**对比表** |
| 5 | 自定义入口预设 Conversion | 改为"抽取通用事实 → 选型 → 按选中类型的必填项检查 → 追问 → 编译"；主图与流水线共用 |
| 6 | 动力学信息在抽取时丢失 | 抽取补充速率方程/参数/设备体积/相态；`kinetic_data` 不再恒为 None；需 CSTR/PFR 时**明确返回不支持** |
| 7 | 含固体碳的 Gibbs 被标成 verified | 当时改为 experimental。**后续变化**：饱和碳路线在验收 `20261003-105341-4467d9c9` 中通过，能力表现在按验收记录标为 verified；普通 Gibbs 直接使用库 Carbon 仍被工具层拒绝（见陷阱 13） |
| 8 | 失败处理不完整 | 成功需**同时**满足退出码、结果结构、证据；超时停止后续工况；stdout/stderr 落盘 |
| 9 | 流水线恢复时状态算错 | 已完成的工况计入完成数，恢复时返回 PASS 而非 FAILED |
| 10 | （部分）放行重整流量后 assumptions 为空 | **未能复现**：流量假设确实被记录。属实的那一半是**默认热边界没有声明**，已修 |

**未采纳的一条**：审查者对第 10 项的描述"assumptions 仍为空"在我们的复现中不成立，请以代码和测试为准，不要沿用。

### 第二轮：同步已验收的工具层（2026-10-03 晚）

工具层新版 `2026-10-03-equilibrium-integration-1` 在验收 `20261003-105341-4467d9c9` 中六个工况全部通过（`ALL_SCENARIOS_PASS`）：甲苯、两个 Gibbs 重整对照、两个 Equilibrium 重整、饱和碳气化。智能体层随之同步：

| 改动 | 说明 |
|---|---|
| 能力表绑定验收哈希 | 智能体记录验收版本和 13 个运行文件的 SHA256，运行时核对；任一不一致，所有 verified 自动降为 experimental |
| 重整选 Equilibrium | 新规则"反应网络闭合 → Equilibrium"；用户明确要 Gibbs、或原文有黑箱特征时仍选 Gibbs |
| 气化可以执行 | Nm³ 和"煤按纯碳"两项以带默认答案的问题确认，确认后走 `flow_input=normal_volume` 与 `solid_carbon=saturation` |
| 追问死循环 | 同一字段只问一次；编译器生成的问题 id 都能路由到对应字段 |
| 问题 id 跨进程稳定 | 用 crc32 取代 `hash()`，暂停后在另一个进程里 `--answer` 也能对上 |
| 关键词误判 | "不可逆"不再算可逆；"请用 Gibbs 反应器"不再算平衡数据；`H2O`、`2.5MPa` 不再被当成方程式 |
| 统一报告 | 单遍流程与状态图共用 `report.py`；透传 Q/K、饱和碳、热负荷口径；标注固相碳；两温度对比解读；CO 收率的氧平衡上限 |
| 命令行 | 默认走状态图；终端逐题作答，回车采用默认值；`--accept-defaults`；`--answer` 不带 `--out` 时自动续上最近的暂停 |

**仍然存在的真实局限**（不要当成新发现）：

- **智能体端到端尚未在工作站上跑过。** 上面六个工况的真机验收是直接调用工具层完成的，不经过智能体。
- **本机所有"执行"类测试都是离线模拟**：使用注入的假模型响应与假适配器，没有连接过 HYSYS。
- 报告层的 Q/K 测试夹具是人工构造的片段，形状与工具层输出一致；工作站真实结果回来后再替换。

---

## 2. 陷阱：这些看起来像 bug，但都是设计意图

### 陷阱 1：气化场景会先追问两项，这是输入确认，不是失败

题目给的是 `80000 Nm3/h`，没有说明标准状态和所指物流；煤也没有给出组成。系统会暂停，问两个问题：

1. `80000 Nm3/h` 指什么。默认答案：单股混合进料的总量，0°C / 101.325 kPa。
2. 煤能否按纯固体碳处理。默认答案：按纯碳处理。

终端里按回车即采用默认值，`--accept-defaults` 会自动采用。确认之后才会生成 spec：进料带 `flow_input: normal_volume` 和两项标准状态，反应器带 `solid_carbon: saturation`。

**为什么不直接用默认值**：这两项决定进料总量和 CO 收率的分母，必须让用户看到并确认。默认答案只是让确认更省事，代码在用户确认之前不会把它写进 spec。

**判据**：暂停期间状态为 `WAITING_INPUT`、退出码 3、**0 个 `.hsc` 文件**；确认后为 `READY`（dry run）或 `PASS`（执行）；同一个问题不会被问第二次。

旧版本在这里直接拒绝执行。那是工具层还没有饱和碳路线时的做法，现在已经不适用。

### 陷阱 2：`experimental` 不是"没做完"，`verified` 也不是写死的

能力状态有三种：`verified` / `experimental` / `unsupported`。

- `verified` 只给在验收 `20261003-105341-4467d9c9` 中真机通过的组合：Conversion 绝热单反应、Gibbs 等温气相、Equilibrium 等温气相、Gibbs 加饱和碳（碳加水进料）。
- 这个判断与工具层文件哈希绑定。`hysys_tools/` 里任何一个运行文件被改动，所有 `verified` 都会自动降为 `experimental`，`reason` 里写明哪个文件不一致。审查时看到不该出现的 `experimental`，先查工具层是否被改过。
- `experimental`：代码路径存在，但没有真机验收，例如等温 Conversion。
- **`unsupported` 才是不执行。**

### 陷阱 3：`baseline_expected.json` 不是"硬编码的期望输出"

它来自**历史真机结果**，只用于**容差比对**（判断本次运行是否退化），不参与计算、
不影响结果。工具层每次都是真的驱动 HYSYS 并读回结果。

验收全部通过时，验收脚本还会导出 `baseline_candidate.json`，记录本轮的完整精度结果。它不会自动覆盖历史基线。

### 陷阱 4：不要在开发机上尝试 `--execute`

**开发机上没有 HYSYS、也没有 `pywin32`**，`--execute` 必然失败。这不是缺陷。

本机可验证的范围是**到"规格通过预检"为止**，默认的 dry run 就能走到。
真机验证请用 `Run-Agent-On-Workstation.cmd`，且**必须在装 HYSYS 的机器上**。

> 同理，不要为了"让测试通过"去装 `pywin32`：`requirements.txt` 刻意把它注释掉，
> 就是为了避免本地环境**看起来**就绪而其实没有 HYSYS。

### 陷阱 5：`hysys_tools/` 是**冻结**的，改动会让验收失效

工具层已通过远程真机验收 `20261003-105341-4467d9c9`（`ALL_SCENARIOS_PASS`）。
`docs/tool-acceptance-20261003-105341.json` 记录了 13 个运行文件的 SHA256，
`reactor_agent/capabilities.py` 运行时会逐个核对。改动工具层会让这份验收不再覆盖当前代码，
智能体的能力表也会随之降级。

`hysys_tools/capabilities.py` 里的文字仍写着远程验收 pending，那是工具层打包前写的；
为保持哈希一致没有改它。验收结论以上面的验收清单为准。

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
运行结束时控制台会打印完整报告；**中文全文同时写入 `explanation.txt`**，控制台显示异常时以文件为准。

### 陷阱 8：向后兼容别名不是死代码

`reactor_agent/graph.py` 里有 `_apply_answers`、`_route_after_plan`、`_question_to_dict`
等私有别名，`nodes/explain.py` 也从 `report.py` 重新导出了原来的私有函数名。它们是为了
**不破坏既有调用方**而保留的，有测试使用。

### 陷阱 9：历史文档与冻结证据不代表当前状态

- `docs/AGENT_IMPLEMENTATION_PLAN.md` 记录的是**规划时**的检查项，大部分已经修复。
- `docs/review-20261003/` 是第一轮外部审查的冻结证据，针对 10-03 上午的版本。其中的
  `verify.py` 对当前代码重跑会报工具层哈希不一致，也可能因模块重构而导入失败，**不要据此报告缺陷**。
- `tool-layer-runs/acceptance-20261002-144131-d442afae` 是旧版工具层的验收（甲苯加 Gibbs 重整，
  当时气化被拒绝），只代表历史版本。

### 陷阱 10：刻意不做的事，不是遗漏

- **不做 CSTR/PFR**：三个场景都未提供动力学参数，属于**已声明**的能力边界。
- **不合并多个 Conversion 反应**：独立校验无法区分各反应各自的转化率，会把总转化率
  冒充成单反应转化率。
- **Equilibrium 只开放等温气相**：液相、含固体、绝热的 Equilibrium 都拒绝，不暗中换成别的模型。
- **Gibbs 不做绝热**：执行器对 Gibbs 反应器不设热边界，这个组合从未验证过。
- **不在用户确认前换算 `Nm3/h`**：见陷阱 1。
- **气化只验收了"碳 + 水"进料**：进料里有其他组分时，饱和碳路线会被预检拒绝。

### 陷阱 11：气化出口的 `LIQUID` 物流是固相碳

HYSYS 把第二股出口固定命名为 `LIQUID`。饱和碳路线下，未反应的碳就在这股物流里，组成为纯 Carbon。
报告会把它标为"固相碳"。不要据此认为模型算出了液态碳。

### 陷阱 12：重整选 Equilibrium 而不是 Gibbs，两者都对

题目写出了重整和水煤气变换两个反应，列出的组分都在这两个反应里，反应网络是闭合的；
没有动力学参数和转化率，出口温度给定。按题目第 8 节，这种情况用 Equilibrium：
平衡常数由 HYSYS 组分 Gibbs 数据按出口温度拟合，并用实际出口的 Q/K 校验。

Gibbs 保留为对照。验收时，同一温度下 Equilibrium 与 Gibbs 的甲烷转化率相差不超过 0.5 个百分点，
热负荷相差不超过 1%。用户原文明确要求 Gibbs 反应器时，系统会选 Gibbs。

### 陷阱 13：气化不是"单台 Gibbs 反应器"，热负荷是外供热

HYSYS V15 库里的 Carbon 带的是气态原子碳的 Gibbs 数据，普通 Gibbs 反应器直接用它会得出不可能的出口，
工具层在求解前拒绝。饱和碳路线是：转化率反应器加仅含气相的 Gibbs 反应器，外层求解使气相碳活度为 1。

题目没有给氧气进料，维持 1400°C 的热量来自外部，报告的热负荷是外供热，不是自热气化。

---

## 3. 五分钟验证（离线，不需要 HYSYS、不需要网络、不需要凭据）

```bash
conda create -n hysys-agent python=3.12 -y && conda activate hysys-agent
python -m pip install -r requirements.txt

# 全部离线测试（13 个套件）
scripts\run-all-tests.cmd
```

预期：**13 个套件全部 exit 0**。各套件的测试数量见脚本输出，本轮修改后的总数记在 `docs/AGENT_FIX_NOTES.md`。

单项跑法：

```bash
python -m hysys_tools.selfcheck
python -m unittest reactor_agent.test_graph
python -m unittest reactor_agent.test_report
python scripts/test_build_submission.py
```

### 需要凭据的端到端（dry run，不会启动任何模拟）

```bash
# PowerShell
$env:TR_KEY = "<key>"; $env:TR_BASE = "https://tokenrhythm.studio/v1"
python -m reactor_agent --scenario toluene --no-input
python -m reactor_agent --scenario smr --no-input
python -m reactor_agent --scenario gasification --no-input
python -m reactor_agent --scenario gasification --accept-defaults
```

预期：

| 命令 | 预期 | 退出码 |
|---|---|---|
| 甲苯 | `READY`，conversion（verified），反应名 `RXN-1`，反应相态 `combined` | 0 |
| 重整 | `READY`，equilibrium（verified），两个工况，案例名分别以 `-710C`、`-600C` 结尾；假设里有自定流量（甲烷 1000 kmol/h，约 128 kt/a）、等温热边界和平衡常数来源 | 0 |
| 气化，`--no-input` | 暂停，两个问题各带默认答案，**未创建任何案例** | 3 |
| 气化，`--accept-defaults` | `READY`，gibbs（verified），spec 含 `flow_input: normal_volume` 和 `solid_carbon: saturation`，组分含 CO2、Methane | 0 |

**没有凭据也能验证大部分逻辑**：几乎所有测试用**注入的假 HTTP transport**，
不联网（这也是刻意的设计，见 `llm.py` 的 `transport` 参数）。

---

## 4. 值得深挖的方向（按我判断的价值排序）

1. **防幻觉检查是否真的拦得住。**
   这是整个项目最重要的一层。试着构造一个"模型编造数值"的场景，确认它**阻塞执行**
   而不是只打个警告，**并且在用户给出无效回答后仍然阻塞**。
   相关测试：`test_pipeline.py::HallucinationBlocksExecution`、
   `test_graph.py::UnreadableAnswerDoesNotReleaseTheValue`、
   `test_extraction.py::Grounding`（单位/物理量判别）。
   **这一层出过两次严重问题**：最初只报警不拦截；修好后又因为 `ask` 无条件清空
   ungrounded 而在无效作答后放行。两条路径都值得再挖。

2. **默认答案是否只在确认后才生效。**
   气化的两个问题带默认答案，这是新加的便利，也是新的风险点。试着找出一条路径，
   让默认答案在用户没有确认时进入 spec。`--no-input` 下气化必须停在暂停状态，不能产生 spec。
   相关测试：`test_graph.py::NormalVolumeAnswers`、`test_cli.py`。

3. **"提问时零副作用"是否成立。**
   请求信息不足时，系统必须**不创建任何案例、不写任何文件**。相关测试：
   `test_graph.py::PausingToAsk`、`test_pipeline.py::BlockedRunsExecuteNothing`。
   试着在真实模型上验证一次（用 `--text` 给一个缺流量的请求）。

4. **"不静默降级"是否处处成立。**
   工具层拒绝的组合，智能体层是否也会拒绝？有没有哪条路径会**悄悄换一个反应器**
   而不告诉用户？看 `reactor_agent/schemas.py` 的 `SelectionDecision.was_substituted()`
   与 `report.py` 里对它的使用（选型替换必须出现在报告里）。

5. **能力表是否真的跟着验收记录走。**
   在仓库的**副本**里改动 `hysys_tools/` 的任意一个运行文件，再查 `combination_status`，
   原来的 `verified` 应全部变成 `experimental`。也可以反过来找：有没有没经过验收的组合被标成 `verified`。

6. **假设是否被如实声明。**
   系统自己选的数值和建模条件必须出现在 `assumptions_we_made` 与 `explanation.txt` 里，并且不会被当成用户给定。
   目前包括：重整进料流量、三种二甲苯等分、默认热边界、Gibbs 候选产物补齐。
   用户确认过的 Nm³ 基准和"煤按纯碳"会单独列为"用户确认"。

7. **失败是否被如实报告。**
   `adapters/hysys_cli.py` 在**没有 `result.json`** 时必须自己写失败记录，而不是
   假设成功。相关测试：`test_adapters.py::SuccessAndFailure`、`test_adapters.py::SuccessNeedsEvidence`。

8. **凭据是否真的不会落盘。**
   `TR_KEY` 只从环境变量读。检查有没有任何路径会把它写进日志、检查点或产物。
   相关测试：`test_llm.py::CredentialHandling`、`test_adapters.py` 的 summary 测试。

---

## 5. 已知局限（**不算新发现**）

请不要把这些当作独立发现，除非你能证明这里的说明**不成立**：

| 局限 | 说明 |
|---|---|
| **智能体端到端未在工作站跑过** | 本机无 HYSYS。智能体层验证到"规格通过预检"为止；工具层六个工况的真机验收不经过智能体 |
| **高温区 Gibbs 数据的可靠性无法由本系统证明** | ln(K) 拟合残差和出口 Q/K 只能说明"按 HYSYS 的 Gibbs 数据计算是自洽的"。早先读回的 25 / 426.85 温区数字单位未确认，不作为 K 或 °C 使用 |
| **气化依赖三项假设** | 煤按纯碳；Nm³ 按 0°C / 101.325 kPa 的进料总量；题目没有氧气进料，所以是外供热 |
| **气化只验收了"碳 + 水"进料** | 其他组成的固体进料不在验收范围内 |
| **报告层的 Q/K 测试夹具是人工构造的** | 形状与工具层一致，等工作站真实结果回来后替换 |
| **抽取依赖外部模型服务** | 需要 `TR_KEY`；模型不可用时明确报错并停止，不猜 |
| **没有图形界面** | 只有 CLI（`python -m reactor_agent`） |

---

## 6. 什么才算真正的发现

欢迎的（举几个例子）：

- **能构造出"编造的值进入了案例"**的输入 → 防幻觉层有洞。
- **能构造出"默认答案未经确认就进入了 spec"**的路径。
- **能构造出"提问时仍创建了案例"**的路径 → 副作用隔离有洞。
- **同一个问题被反复追问**，或者回答之后仍然暂停 → 追问路由有洞。
- **能力表标错**：没经过验收的组合标成 `verified`，或工具层改动后仍显示 `verified`；
  也包括被拒绝的组合其实可以安全执行。
- **某个必填字段漏检**（例如改了 schema 后 `REQUIRED_BY_KIND` 没跟上）。
- **`REPORT.md` / `README.md` 里的某个数字与证据不符**。
   请你**逐条核对**：报告里的每个数字都应该能在 `tool-layer-runs/` 或测试输出里找到。
- **假设被当成了用户给定**（没出现在 `assumptions_we_made` 里）。
- **选型替换没有出现在报告里**。
- **凭据有落盘路径**。

请**谨慎报告**的：

- 把陷阱 1–13 里的任何一条当作缺陷。
- 基于"开发机没有 HYSYS"得出的结论。
- 建议在开发机上装 `pywin32` 或改 `hysys_tools/`。
- 把 `experimental` 等同于"未完成"。
- 用 `docs/review-20261003/verify.py` 对当前代码重跑得出的结论。

---

## 7. 报告发现时的要求

1. **给出可复现的命令**与**实际输出**，不要只给判断。
2. 区分"**我验证了**"与"**我推断**"。
3. 如果是设计意图问题（你认为某个设计决定是错的），请说明**代价**是什么，
   以及**你会怎么权衡**——这个项目里有若干"选择了保守"的地方是刻意的。
4. 涉及 `hysys_tools/` 的改动建议，请说明**需要重新验收哪些行为**。
