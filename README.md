# AI 驱动的化工反应器建模

> **2026-10-03 状态**：工具层已通过验收 `20261003-105341-4467d9c9`（`ALL_SCENARIOS_PASS`）；
> **智能体层已同步**到这个工具层，离线测试全部通过，**端到端真机运行待执行**。
> 第一次接触本项目请先读 [docs/REVIEW_BRIEF.md](docs/REVIEW_BRIEF.md)。

自然语言描述 → **自主判断反应器类型并说明理由** → 在 HYSYS 中实际创建并求解 →
独立校验 → 返回可解释的计算结果。

考核要求见 [题目原文](../AI化工反应器建模实战考核(1).md)。本仓库分两层：

| 层 | 作用 | 是否依赖 HYSYS |
|---|---|---|
| `hysys_tools/` | 把一个严格的 JSON 规格变成真实 HYSYS 案例，并独立校验结果 | 真机执行时需要 |
| `reactor_agent/` | 把自然语言变成规格、把结果变成工程解释 | 不需要 |

## 当前状态

**工具层已通过真机验收。**

```
run_id 20261003-105341-4467d9c9   (远程 Python 3.12.4, Windows build 19045)
status ALL_SCENARIOS_PASS
```

| 工况 | 模型 | 结果 |
|---|---|---|
| 甲苯歧化 10000 kg/h、380°C、2.5 MPa | Conversion（绝热） | 转化率 49.99999999999999% |
| 甲烷蒸汽重整 710°C | Equilibrium（等温气相） | 已验收；同温度 Gibbs 对照的 CH₄ 54.035809% |
| 甲烷蒸汽重整 600°C | Equilibrium（等温气相） | 已验收；同温度 Gibbs 对照的 CH₄ 30.352385% |
| 水煤浆气化 1400°C | Gibbs + `solid_carbon=saturation` | 已验收（碳 + 水进料） |

验收清单（13 个运行文件的 SHA256）：`docs/tool-acceptance-20261003-105341.json`；
`reactor_agent/capabilities.py` 运行时会逐个核对，**改一个字节所有 `verified` 自动降为
`experimental`**。证据：`tool-layer-runs/acceptance-20261003-105341-4467d9c9/`。

历史验收 `20261002-144131-d442afae`（甲苯 + Gibbs 重整，当时气化被拒绝）见
`tool-layer-runs/acceptance-20261002-144131-d442afae/`，**只代表历史版本**。

**Agent 层已打通自然语言入口**（离线测试通过；带模型 key 的 dry run 与真机运行待执行）：

| 场景 | 结果 |
|---|---|
| 甲苯歧化 | `READY` —— Conversion（verified），反应名 `RXN-1`、相态 `combined` |
| 甲烷蒸汽重整 | `READY` —— Equilibrium（verified），两个工况，案例名 `smr-710C`/`smr-600C`，自定流量甲烷 1000 kmol/h（总 3700 kmol/h，约 128 kt/a） |
| 水煤浆气化 | 先 `WAITING_INPUT`（两题都带默认答案）；回车采用默认后 `READY` —— Gibbs（verified），spec 含 `flow_input: normal_volume` 与 `solid_carbon: saturation` |

**离线测试全部通过**：工具层 122 项自检 + 184 项回归，Agent 层 373 项，打包器 17 项；
`scripts\run-all-tests.cmd` **14 个套件**全绿。各套件数量见 `docs/AGENT_FIX_NOTES.md`。

## 目录结构

```
.
├── hysys_tools/              # 工具层（真机验收通过；改动需重新验收）
│   ├── core.py               # 契约、组分/物性包映射、单位换算、元素守恒
│   ├── precheck.py           # 离线预检：不碰 HYSYS，毫秒级，一次列出全部问题
│   ├── reactor.py            # 三个反应器驱动 + COM 调用序列 + 案例所有权保护
│   ├── equilibrium.py        # Equilibrium：ln K 拟合与出口 Q/K 校验
│   ├── saturation.py         # 饱和碳路线：转化率反应器 + 气相 Gibbs + 外层求解
│   ├── native_flow.py        # 标准体积（Nm3/h）进料的换算
│   ├── validate.py           # 独立校验：守恒、转化率、CO 收率、干基分数
│   ├── main.py               # CLI 与 build_case
│   ├── examples.py           # 考核场景的示例规格
│   ├── capabilities.py       # 机器可读能力表（其文字仍是打包前的 pending，见 REVIEW_BRIEF 陷阱 5）
│   ├── remote_check.py       # 真机验收运行器（互斥、超时、基线比对、证据 ZIP）
│   ├── selfcheck.py          # 122 项离线自检
│   └── test_reliability.py   # 184 项可靠性回归
├── reactor_agent/            # 自然语言入口
│   ├── schemas.py            # 三层数据契约（pydantic）
│   ├── capabilities.py       # 按验收记录核对工具层哈希，再查「模型+热边界+相态+反应数」
│   ├── selection.py          # 选型规则（题目第 8 节），含"反应网络闭合 → Equilibrium"
│   ├── compiler.py           # ModelingPlan → hysys-agent/spec/1
│   ├── report.py             # 唯一的报告渲染器（图与单遍流程共用）
│   ├── llm.py                # 模型客户端：降级链、控速、enable_thinking=false
│   ├── extraction.py         # 模型侧契约 + 防幻觉检查（grounding）
│   ├── normalize.py          # 确定性归一化：单位、组分名、反应式、工况
│   ├── pipeline.py           # 单遍流程，含 dry run
│   ├── graph.py              # LangGraph 主图装配 + 检查点 + 产物输出
│   ├── nodes/                # 每个阶段一个模块
│   │   ├── state.py          #   图状态（JSON 可序列化，供 SQLite 检查点）
│   │   ├── intake.py         #   自然语言 → 已核对的事实（唯一调用模型的节点）
│   │   ├── plan.py           #   事实 → 归一化/选型/编译/预检（确定性）
│   │   ├── ask.py            #   暂停追问（无任何副作用）
│   │   ├── execute.py        #   唯一有副作用的节点，受 plan 的状态门控
│   │   ├── explain.py        #   调用 report.py 渲染中文解释
│   │   └── answers.py        #   把用户作答并回事实（宽松解析）
│   ├── adapters/             # 子进程执行工具层 + 执行台账
│   ├── streamlit_app.py      # 中文聊天界面（Streamlit）
│   ├── chat_service.py       # 复用原有图与检查点的聊天交互
│   ├── ui_backend.py         # 会话、模型设置、检查点和产物管理
│   ├── process_trace.py      # 实际执行事件与过程记录
│   ├── __main__.py           # CLI：python -m reactor_agent（默认走状态图）
│   └── test_*.py             # 智能体离线回归测试
├── docs/
│   ├── TOOL_REFERENCE.md         # 工具层接口参考（agent 可直接读）
│   ├── REMOTE_VALIDATION.md      # 远程验收步骤与判读
│   ├── EQUILIBRIUM_INTEGRATION.md # Equilibrium 与饱和碳路线的接入说明
│   ├── REVIEW_BRIEF.md           # 给独立审查方的说明（先读它）
│   ├── AGENT_FIX_NOTES.md        # 本次智能体层修复逐步记录（含偏离说明）
│   ├── GWOA_MIGRATION.md         # 智能体设计的迁移来源
│   └── AGENT_IMPLEMENTATION_PLAN.md  # Agent 搭建规划（规划时的检查记录）
├── scripts/
│   ├── build_release.py      # 生成允许清单内的远程测试 ZIP
│   ├── probe_llm.py          # 探测模型端点能力
│   ├── bench_llm.py          # 模型准确率与速度基准
│   ├── pick_model.py         # 批量模型选型（TR_MODELS=a,b,c）
│   └── run-all-tests.cmd     # 一键发现并运行全部离线测试
├── verification/            # 验证材料；legacy-model-evaluation 为历史模型记录
├── tool-layer-runs/          # 运行产物与验收证据
├── baseline_expected.json    # 历史真机结果，用于容差比对（不是本版输出）
├── PROJECT_PLAN.md           # 项目计划与当前进度
├── TECH_STACK.md             # 技术栈决策与理由
├── requirements.txt
├── README.md
└── Run-*.cmd                 # 双击入口：Agent 真机流程 / 仅离线检查 / 工具层验收
```

本地运行产物 `agent-runs/`、模型探测输出 `_demo/`、归档 `_archive/` 与缓存不进入 Git。
历史模型输出集中在 `verification/legacy-model-evaluation/`，当前验收证据保留原路径。

## 环境与安装

本机与远程使用**同一个 Python 版本**（3.12），避免"本地能跑、远程不能"。

```bash
conda create -n hysys-agent python=3.12 -y
conda activate hysys-agent
python -m pip install -r requirements.txt
```

工具层的离线部分**只依赖标准库**；`pydantic` 与 `langgraph` 属于 Agent 层。
真机执行 HYSYS COM 需要 `pywin32`（在 `requirements.txt` 中注释说明，仅工作站安装）。

## 快速开始

**不连接 HYSYS**（任何机器都可以）：

```bash
python -m hysys_tools.selfcheck                              # 122 项自检
python -m unittest hysys_tools.test_reliability              # 184 项回归
python -m unittest reactor_agent.test_selection              # 选型规则
python -m unittest reactor_agent.test_compiler               # 规格编译
python -m unittest reactor_agent.test_report                 # 报告渲染
python -m hysys_tools --list-capabilities                    # 能力表（JSON）
python -m hysys_tools --validate-only --spec specs\case.json # 离线预检
python -m hysys_tools.remote_check --offline                 # 离线验收（产出证据 ZIP）
```

一键跑全部：`scripts\run-all-tests.cmd`

## 用自然语言驱动（Agent 层）

**默认即状态图**：请求信息不足时会暂停提问，而不会带着猜测往下走。

```bash
# 凭据只从环境变量读，不写入任何文件
# PowerShell:  $env:TR_KEY = "<key>"

python -m reactor_agent --list-scenarios          # 三个内置场景
python -m reactor_agent --scenario toluene        # 默认 dry run：不启动任何模拟
python -m reactor_agent --scenario gasification   # 终端逐题作答（回车 = 采用默认答案）
python -m reactor_agent --scenario gasification --accept-defaults
python -m reactor_agent --scenario gasification --no-input     # 只打印问题，退出码 3
python -m reactor_agent --scenario toluene --execute    # 真的驱动 HYSYS
python -m reactor_agent --text "甲苯进料10000kg/h，380℃，2.5MPa，转化率50%"
python -m reactor_agent --file request.txt --basis mass_fraction
python -m reactor_agent --scenario toluene --single-pass  # 旧的单遍流程，不能追问
```

**默认不执行任何模拟**（dry run）：它会真实地抽取、归一化、选型、编译并预检，
然后打印"本来会运行"的规格。这也是本机（无 HYSYS）验证前半段的方式。

**暂停与恢复**：信息不足时运行会暂停并在运行目录写下 `paused.json`，**此时没有创建任何
案例**；`--answer` 不带 `--out` 时会**自动找到最近一次暂停的运行**并续上：

```bash
python -m reactor_agent --scenario gasification --no-input
#   PAUSED for clarification. Nothing was executed.
#   ? 进料流量 80000 Nm3/h 按什么理解？默认：单股混合进料的总量，标准状态 0°C / 101.325 kPa。…
#     id: q-volumetric-flow    default: 总进料，0°C/101.325 kPa
#   ? 煤能否按纯固体碳处理？默认：按纯碳处理。…
#     id: q-coal-definition    default: 按纯碳处理

python -m reactor_agent --scenario gasification \
       --answer q-volumetric-flow=默认 --answer q-coal-definition=按纯碳处理
```

**在装有 HYSYS 的工作站上**（本机没有 HYSYS，这一步必须由你在工作站执行）：

1. 启动 HYSYS，处理掉弹窗，并关闭遗留的案例。
2. 在**当前窗口**设置凭据：`set TR_KEY=<your key>`（只从环境变量读，不落盘）。
3. 双击 `Run-Agent-On-Workstation.cmd` —— 四步：真机跑甲苯、真机跑重整（两个工况）、
   真机跑气化（饱和碳路线），最后 dry run 气化做交互演示（两次回车采用默认答案）。
4. 回传它打印的 `agent-runs\workstation-*\` 整个目录。

只验收工具层（不经过 Agent）则用 `Run-Remote-Validation.cmd`。

判读标准见 [docs/REMOTE_VALIDATION.md](docs/REMOTE_VALIDATION.md)。

**运行单个规格**：

```bash
python -m hysys_tools --spec specs\case.json --folder runs\unique-run-id
```

> 每次必须使用**全新的输出目录**。同一 HYSYS 实例只允许串行调用。
> 工具层只会修改和关闭**本次自己创建的**案例，不会通过活动文档定位或改动用户原有的案例。

## 中文聊天界面（Streamlit）

像聊天一样提交模拟题目，在消息中回答追问、查看结果和下载方案。复用 CLI 的同一套
提取、校验、反应器选型、LangGraph 检查点、HYSYS 执行与报告逻辑。

```bash
conda activate hysys-agent
python -m pip install -r requirements.txt
python -m streamlit run reactor_agent/streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
```

也可以直接双击 **Start-Demo.bat** 或 **Run-Agent-UI.cmd**，自动打开
http://127.0.0.1:8501。启动器优先使用 `HYSYS_AGENT_PYTHON`，随后寻找用户目录下的
Miniconda3 / anaconda3 的 `hysys-agent` 环境、当前激活环境，最后使用 PATH 中的 Python。
自定义安装路径时，可设置 `HYSYS_AGENT_PYTHON` 为该环境的 python.exe；端口可用
`Start-Demo.bat 8502` 指定。

- 侧栏提供新建对话、当前会话的对话列表、中文运行选项和模型连接设置。
- 每条任务的“完整运行过程”实时显示收到输入、模型抽取、校验/选型/编译预检、
  追问确认、各工况 HYSYS 调用与返回、报告生成。展开阶段详情可查看抽取 JSON、
  选型依据、假设、编译规格和返回结果；过程记录可下载为 `process.json`。
  过程来自实际图事件，追问时暂停、方案模式未执行 HYSYS 都会明确显示。
  校验、选型和编译预检属于同一个后端节点，作为同一阶段展示；界面不显示模型内部思考过程。
- 默认“生成模拟方案”；真实计算选择“执行 HYSYS 模拟”并确认工作站准备就绪。
- 报告区分题目给定条件、系统默认与推导值；各出口流股逐项列出组分摩尔流量，
  按该流股总摩尔流量乘摩尔分数推算，气相与固相分别报告。
- 三个题目示例只填入输入框，点击发送才会开始请求。
- 追问在消息中的表单里填写，默认答案会预填；输入“默认”可采用全部默认答案。
  单个追问也可直接在聊天框回复，多个问题请用表单回答。
- 页面组件更新、下载、切换对话不会调用模型或重复执行案例。重新加载浏览器会建立新会话，
  对话列表不跨会话保存；检查点和模拟文件仍保存在 `agent-runs/chat-*/` 下。
- API Key 使用密码框，仅保存在当前会话内存中，不写入模拟文件、检查点或对话记录。
  页面填写优先于环境变量和项目根目录 `.env`：

  ```
  TR_BASE=https://tokenrhythm.studio/v1
  TR_MODEL=qwen3.7-flash
  TR_KEY=<your key>
  ```

  `.env` 已被 Git 和提交包排除；不要把它放在 `reactor_agent/` 目录下。
- 启动器只监听本机 `127.0.0.1`。前端统一使用 Streamlit，旧版 HTML 页面与 HTTP 服务已移除。


## 能力范围与边界

每一条都由 `reactor_agent/capabilities.py` 按「模型 + 热边界 + 相态 + 反应数 + 是否含固体」
逐格给出，并且只在工具层哈希仍与验收记录一致时才算 `verified`。

**已验收（`verified`）**

| 组合 | 说明 |
|---|---|
| Conversion，单反应，绝热 | 甲苯歧化 |
| Equilibrium，等温，气相 | 甲烷蒸汽重整（两个工况）；平衡常数按出口温度拟合，出口 Q/K 校验 |
| Gibbs，等温，气相 | 作为重整的对照方案 |
| Gibbs，等温，含固体碳 | 气化：走 `solid_carbon=saturation` 组合流程（**不是**单台库 Carbon 的 Gibbs） |

**不支持**（工具层会明确拒绝，不会静默降级）

| 组合 | 原因 |
|---|---|
| CSTR / PFR | 没有实现。三个场景都未提供动力学参数，属于已声明的能力边界 |
| 多个 Conversion 反应 | 独立校验无法区分各反应各自的转化率，会把总转化率冒充成单反应转化率 |
| 绝热 Equilibrium | 无法设置 Ln(K) 来源；只验收了等温 |
| 液相或含固体的 Equilibrium | Equilibrium 只接受气相反应 |
| Gibbs + 绝热 | 执行器对该组合不设热边界，从未验证 |

**实验性**（代码路径存在，但没有通过验收的真机记录）：Conversion 等温、转化率随温度变化的
系数。

**气化需要确认的两项**（确认前不会创建任何案例）

1. **`80000 Nm3/h` 指的是哪一股物流、什么标准状态**。煤是固体、水是液体，Nm³ 只对气体
   有意义。默认答案是把 Nm³ 理解为**单股混合进料的总量，0°C / 101.325 kPa**；也可以给出
   其他标准状态，或直接改给质量或摩尔流量。不同解释会给出完全不同的 CO 收率，所以必须由
   用户确认，而不是系统自己选定。
2. **煤能否按纯固体碳处理**。题目只说"灰分不做考虑"，未给煤的组成；按元素分析建模需要
   工具层另行扩展（目前只验收了"碳 + 水"进料）。默认答案是"按纯碳处理"，这会影响碳平衡
   与 CO 收率的分母，因此单独声明。

**已知物理限制**：HYSYS 组分 Gibbs 数据温区上限读回为 426.85 K，而重整工况需要
873–983 K（600/710°C），因此平衡常数可能是**外推值**。工具层在结果中输出该
warning，报告不得隐瞒。Q/K 接近 1 只能说明出口与该温区拟合出的平衡关系自洽，
**不能证明**高温区 Gibbs 数据本身准确。

## 模型服务（Agent 层）

端点与凭据**运行时从环境变量读入，不写入任何文件**：

```bash
# 本地开发（临时，仅当前终端会话有效）
export TR_BASE=https://tokenrhythm.studio/v1     # PowerShell: $env:TR_BASE=...
export TR_KEY=<your key>                          # PowerShell: $env:TR_KEY=...
export TR_MODEL=qwen3.7-flash                     # 可选，这是默认值
# 注意：调用时需带 {enable_thinking: false}。实测关掉推理让同模型
# 快约 7 倍（24.5s -> 3.6s）且准确率更高，原理见 TECH_STACK.md 4.3 节。

# 探测端点能力（结果写入 _demo/，不含凭据）
python scripts/probe_llm.py
# 对比候选模型的速度与准确率（串行 + 重试；并发会触发限流）
python scripts/bench_llm.py
```

**给模型的契约与内部契约不是同一份**。内部三层契约（`reactor_agent/schemas.py`）用
pydantic 完整表达；**给模型**的抽取契约另行简化：扁平、重复结构用数组而非开放字典
（`species: [{name, coefficient}]` 而不是 `dict[str, float]`），并要求模型**照抄**数字
与单位、把它不确定的东西放进 `missing_information`。

原因见 [TECH_STACK.md](TECH_STACK.md) 第 4.2 节：开放字典会让模型自己发明键名，
实测输出过 `"stoichiometry": { ": ": -2 }` 这样的乱码。归一化（`"度"`→`C`、
`100`→`1.0`）由 `compiler.py` 和 `precheck.py` 的确定性代码完成。

## 交付与打包

```bash
# 提交包（两层源码 + 文档 + 验收证据 + 逐文件 SHA256 清单）
python scripts/build_submission.py
python scripts/build_submission.py --out D:\somewhere\submission.zip

# 提交前预检（凭据扫描 + 大文件扫描），不执行任何 git 命令
python scripts/prepare_git.py

# 只发给工作站、用于真机验收的工具层小包
python scripts/build_release.py
```

`build_submission.py` 用**允许清单**而非黑名单：目录逐个列出，所以新出现的文件不会
被"顺手"打包进去。它**绝不包含** `.rdp`（含工作站地址）、`.hsc`（大二进制）、
`.pyc`、`_archive/`；并且**在写盘前扫描凭据**——一旦匹配到 key 就**中止打包**而不是
打个警告。生成后会把 ZIP 读回来逐个复核 SHA256。

## 文档

- **项目报告（1–2 页）**：[REPORT.md](REPORT.md)
- **给独立审查方的说明**：[docs/REVIEW_BRIEF.md](docs/REVIEW_BRIEF.md)
  —— 交接检查前请先读它：里面有**看起来像 bug 但属于设计意图**的十几条陷阱
  （气化为什么先问两个问题、`experimental` 的含义、为什么不改 `hysys_tools/`）、分钟级验证步骤，
  以及已声明的局限
- **本次智能体层修复的逐步记录**：[docs/AGENT_FIX_NOTES.md](docs/AGENT_FIX_NOTES.md)
  —— 每一步的实际改动、偏离计划的地方及理由、最终测试数量
- **项目计划与当前进度**：[PROJECT_PLAN.md](PROJECT_PLAN.md)
- **技术栈决策与理由**：[TECH_STACK.md](TECH_STACK.md)
- 工具层接口与错误处理：[docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)
- Equilibrium 与饱和碳路线的接入说明：[docs/EQUILIBRIUM_INTEGRATION.md](docs/EQUILIBRIUM_INTEGRATION.md)
- 远程验收流程：[docs/REMOTE_VALIDATION.md](docs/REMOTE_VALIDATION.md)
- 第一轮外部审查的冻结证据：[docs/review-20261003/](docs/review-20261003/)
  （针对 10-03 上午的版本，**不代表当前状态**）
- **智能体设计的迁移来源**：[docs/GWOA_MIGRATION.md](docs/GWOA_MIGRATION.md)
- Agent 搭建规划（**规划时的历史记录，不代表当前状态**）：[docs/AGENT_IMPLEMENTATION_PLAN.md](docs/AGENT_IMPLEMENTATION_PLAN.md)

`baseline_expected.json` 来自历史真机结果，用于容差比对，**不是本版计算输出**。
证据 ZIP 会保存本次代码哈希、spec、日志、结果以及成功保存的 HSC。
