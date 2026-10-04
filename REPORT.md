# AI 化工反应器建模实战 —— 项目报告

## 一、做了什么

一个能接收自然语言、**自主判断反应器类型并说明理由**、在 HYSYS 中创建并求解、
最后返回可解释结果的系统。分两层，边界明确：

| 层 | 职责 | 是否依赖 HYSYS |
|---|---|---|
| `hysys_tools/` | 把严格的 JSON 规格变成真实 HYSYS 案例，并独立校验结果 | 真机执行时需要 |
| `reactor_agent/` | 自然语言 → 规格 → 工程解释 | 不需要 |

**关键架构决定**：模型的职责**止于"把自然语言变成事实"**。选型由
`selection.py` 的确定性规则从事实推导；规格由 `compiler.py` 生成；任何
模型产物都要经过确定性 Python 才能变成 COM 调用、文件路径或子进程参数。

流程分为三层契约：

```
ProcessRequest   用户到底说了什么，每个字段的来源；允许缺失（缺失也是信息）
ModelingPlan     我们决定怎么建模：反应器、组分、热边界、假设、各工况
ToolSpec         hysys-agent/spec/1，工具层接受的严格文档
```

## 二、验证到什么程度

### 真机验收（工具层，已完成）

```
run_id 20261003-105341-4467d9c9   (远程 Python 3.12.4, Windows build 19045)
status ALL_SCENARIOS_PASS
```

| 工况 | 结果 |
|---|---|
| 甲苯歧化 10000 kg/h、380°C、2.5 MPa | Conversion 绝热，转化率 **49.99999999999999%**（与历史基线逐位相同）|
| 甲烷蒸汽重整 710°C | Equilibrium 等温气相，PASS；同温度 Gibbs 对照 CH₄ 54.035809%、H₂ 1942.812 kmol/h |
| 甲烷蒸汽重整 600°C | Equilibrium 等温气相，PASS；同温度 Gibbs 对照 CH₄ 30.352385%、H₂ 1163.670 kmol/h |
| 水煤浆气化 1400°C | Gibbs + `solid_carbon=saturation`，PASS |

验收清单（13 个运行文件的 SHA256）在 `docs/tool-acceptance-20261003-105341.json`；
`reactor_agent/capabilities.py` 运行时会核对每一个哈希，任何不一致都会把
`verified` 降为 `experimental`。历史验收 `20261002-144131-d442afae`
（甲苯 + Gibbs 重整，当时气化被拒绝）只代表历史版本。

### 端到端（Agent 层）

`python -m reactor_agent --scenario <名称>`（默认即状态图，可暂停追问）：

| 场景 | 结果 |
|---|---|
| 甲苯 | `READY` → 规格通过预检，元素守恒（C 14=6+8×3×⅓，H 16=6+10×3×⅓）|
| 重整 | `READY`，**两个工况各自通过预检**，选 Equilibrium；自动记录假设：自定流量甲烷 1000 kmol/h（总 3700 kmol/h，约 128 kt/a）|
| 气化 | 先 `WAITING_INPUT`（两题都带默认答案），确认后 `READY`：`flow_input=normal_volume`、`solid_carbon=saturation` |

**离线测试 678 项全部通过**（工具层 122 自检 + 184 回归；Agent 层 355；打包器 17）。
带模型凭据的三个场景 dry run 与工作站真机运行**待执行**，见下。

## 三、几个值得说的设计决定

### 1. 防幻觉检查必须"拦截"，不能只"报警"

模型曾被要求抽取一个**原文没有给出**的流量，它编造了 `feed_total = 1 mol/s`。
最初的实现把 grounding 失败记录成一条 problem 就继续走——**编造的值留在事实里，
会一路进入案例**。报告一句"可能有编造"没有任何保护作用。

现在它变成**阻塞问题**：`q-ungrounded:feed_total`，用户确认前不执行任何模拟。
这是整个项目里最有价值的一次修复。

### 2. "信息不足"与"授权自定"是两件事

- 气化题的 `80000 Nm3/h` 是**真的信息不足**：Nm³ 只对气体有意义，而煤是固体、
  水是液体，且未说明标况与所指流股。系统**先提问、不自行选定解释**；两个问题都带
  默认答案（"单股混合进料总量，0°C/101.325 kPa"和"按纯碳处理"），用户确认后才编译规格，
  确认前不创建任何案例。
- 重整题的"进料流量可以自定"是**授权我们选**。系统锚定到**含碳的反应物**（甲烷，而不是
  比例最大的水）取 1000 kmol/h，推出总流量 3700 kmol/h，**记为 `agent_default` 假设**并在
  报告中声明。

把这两者混为一谈，要么会拦住用户明确授权的运行，要么会把猜测伪装成事实。

### 3. 字段名必须覆盖原文语义，否则模型会静默丢弃

重整原文的"压力 13.5 bar"是**工况压力**而非进料压力。schema 里只有
`feed_pressure` 时，模型**无处安放**这个值，直接丢成 null。补上
`case_pressures` 后三次全部提取到。

**教训：字段名与语义不匹配时，模型不报错，它会静默丢弃。**

### 4. 给模型的契约要和内部契约分开

把完整的三层契约交给模型，它输出了键名乱码：

```json
"stoichiometry": { ": ": -2 },
"flows": { ": {": 10000, "": 380 }
```

原因是 `dict[str, float]` 在 JSON Schema 里变成 `additionalProperties`——开放字典，
`strict` 模式下模型必须**自己发明键名**。改为数组化后结构完全正确。

同理，反应式原本要求模型维护 `reactants` / `products` **两个列表**并同时遵守正负号
约定。实测模型把产物也塞进了 `reactants`，反应式配平结果为零。改为**单一带符号列表**
后无歧义。

### 5. 关掉推理让同一个模型又快又准

`qwen3.7-flash` 是推理模型。默认配置下 token 全花在 `reasoning_tokens` 上，
输出被截断：

| 配置 | 重整场景 | 耗时 | reasoning tokens |
|---|---|---|---|
| 默认（推理开） | 60% | 28.4 s | 2646 |
| **`enable_thinking: false`** | **80%** | **3.6 s** | **0** |

三轮复测：甲苯 100%/100%/100%，重整 100%/20%/100%，**平均 87%、平均 3.6 秒**
（对照默认配置 0.80 / 24.5 秒）。**想得多反而答得少。**

## 四、边界与不足（不隐瞒）

**已真机验证**：Conversion 单反应绝热、Equilibrium 等温气相、Gibbs 等温气相，
以及 Gibbs + 饱和碳路线（气化，碳 + 水进料）。

**工具层明确拒绝**（不静默降级）：

| 组合 | 原因 |
|---|---|
| 绝热 Equilibrium | Ln(K) 来源只能在等温下设置 |
| 液相或含固体的 Equilibrium | Equilibrium 只接受气相反应 |
| Gibbs + 绝热 | 执行器对该组合不设热边界，从未验证 |
| 多个 Conversion 反应 | 独立校验无法区分各反应各自的转化率 |
| CSTR / PFR | 未实现；三个场景均未提供动力学参数 |

**已知物理限制**：HYSYS 组分 Gibbs 数据温区上限读回 **426.85 K**，而重整工况需要
873–983 K（600/710°C），因此平衡常数**可能是外推值**。工具层在结果中输出该
warning。出口 Q/K 接近 1 只说明与该温区拟合出的平衡关系自洽，**不能证明**高温区
Gibbs 数据本身准确。

**未完成**：

- **真机上的 Agent 端到端**尚未跑过：本机没有 HYSYS，Agent 层验证到"规格通过
  预检"为止。执行适配器、台账、暂停恢复、网页界面都有离线测试覆盖，但没有在真实
  工作站上跑过一次完整链路。跑法与判读见 `Run-Agent-On-Workstation.cmd` 与
  `docs/REMOTE_VALIDATION.md`。
- **带 `TR_KEY` 的三个场景 dry run** 需要在有凭据的环境里执行（见 `docs/REVIEW_BRIEF.md`
  第 3 节）；本条记录写于一个没有凭据的会话。

## 五、可复现性

```bash
conda create -n hysys-agent python=3.12 -y && conda activate hysys-agent
python -m pip install -r requirements.txt

# 全部离线测试（不需要 HYSYS、不需要网络）
scripts\run-all-tests.cmd

# 端到端（dry run，不会启动任何模拟）
$env:TR_KEY = "<key>"
python -m reactor_agent --scenario toluene

# 带追问的场景：回车采用默认答案，或让它全自动
python -m reactor_agent --scenario gasification
python -m reactor_agent --scenario gasification --accept-defaults

# 本机网页界面（只监听 127.0.0.1；key 在页面上填，不落盘）
python -m streamlit run reactor_agent/streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false

# 真机（在装有 HYSYS 的工作站上）
python -m reactor_agent --scenario toluene --execute --accept-defaults
```

**凭据在界面填写，或从环境变量及项目根目录 `.env` 读取**（`TR_BASE` / `TR_KEY` / `TR_MODEL`），程序不把凭据写入产物、
日志或检查点；由测试强制保证。

## 六、AI 协作方式

本项目的开发过程本身是一次 AI 协作实践，关键做法：

- **每个结论都用实验验证，不靠推断**。模型选型、`enable_thinking`、限流行为、
  prompt 效果全部有实测数据；猜错的假设（如"qwen 慢是能力问题"）都被实验推翻。
- **失败被当作信息**。真机跑出"甲苯转化率 0%"时没有调参掩盖，而是定位到
  HYSYS 里残留 96 个案例，重启后恢复 49.99999999999999%。
- **把踩过的坑写成测试**。反应式正负号、`case_pressures`、百分比误读、幻觉值
  拦截——每一个都变成了回归测试，防止复发。
- **不隐瞒边界**。工具层拒绝的组合、未验证的路径、外推的平衡常数都写进文档，
  而不是让它们看起来像成功了。
