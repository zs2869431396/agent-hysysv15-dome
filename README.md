# AI 驱动的化工反应器建模

> **2026-10-03 工具层更新**：重整正式示例改用 Equilibrium，饱和碳气化进入主验收；新版已通过离线测试，完整真机验收待运行。请先阅读 [接入与远程运行说明](docs/EQUILIBRIUM_INTEGRATION.md)。下方旧验收表描述历史版本，不能作为新版通过的证明；智能体入口同步另行进行。

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
run_id 20261002-144131-d442afae   (远程 Python 3.12.4, Windows build 19045)
status BASELINE_PASS_GASIFICATION_STILL_BLOCKED
```

| 工况 | 模型 | 结果 | 与历史基线 |
|---|---|---|---|
| 甲苯歧化 10000 kg/h、380°C、2.5 MPa | Conversion（绝热） | 转化率 49.99999999999999% | 逐位相同 |
| 甲烷蒸汽重整 710°C | Gibbs（等温） | CH₄ 54.035809%、H₂ 1942.812 kmol/h | 逐位相同 |
| 甲烷蒸汽重整 600°C | Gibbs（等温） | CH₄ 30.352385%、H₂ 1163.670 kmol/h | 逐位相同 |
| 水煤浆气化（原题输入） | — | 预检拒绝，**未创建案例** | 按设计 |

证据：`tool-layer-runs/acceptance-20261002-144131-d442afae/`（含 `summary.json`、
各工况 `result.json`/`steps.json`、健康检查，以及远程代码哈希）。

**Agent 层已打通自然语言入口**（真实模型调用，本机无 HYSYS 故止于预检）：

| 场景 | 结果 |
|---|---|
| 甲苯歧化 | `READY` —— 规格通过预检，元素守恒 |
| 甲烷蒸汽重整 | `READY` —— 两个工况各自通过预检，自动记录假设（进料流量 1370.37 kmol/h）|
| 水煤浆气化 | `WAITING_INPUT` —— **未创建任何案例**，列出待澄清问题 |

**离线测试 403 项全部通过**：工具层 144 + Agent 层 242 + 打包器 17。

## 目录结构

```
.
├── hysys_tools/              # 工具层（真机验收通过；改动需重新验收）
│   ├── core.py               # 契约、组分/物性包映射、单位换算、元素守恒
│   ├── precheck.py           # 离线预检：不碰 HYSYS，毫秒级，一次列出全部问题
│   ├── reactor.py            # 三个反应器驱动 + COM 调用序列 + 案例所有权保护
│   ├── validate.py           # 独立校验：守恒、转化率、CO 收率、干基分数
│   ├── main.py               # CLI 与 build_case
│   ├── examples.py           # 三个考核场景的示例规格
│   ├── capabilities.py       # 机器可读能力表
│   ├── remote_check.py       # 真机验收运行器（互斥、超时、基线比对、证据 ZIP）
│   ├── selfcheck.py          # 110 项离线自检
│   └── test_reliability.py   # 34 项可靠性回归
├── reactor_agent/            # 自然语言入口
│   ├── schemas.py            # 三层数据契约（pydantic）
│   ├── capabilities.py       # 按「模型+热边界+相态+反应数」查能力状态
│   ├── selection.py          # 选型规则（题目第 8 节）
│   ├── compiler.py           # ModelingPlan → hysys-agent/spec/1
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
│   │   ├── explain.py        #   汇总为中文解释，声明我方假设
│   │   └── answers.py        #   把用户作答并回事实（宽松解析）
│   ├── adapters/             # 子进程执行工具层 + 执行台账
│   ├── __main__.py           # CLI：python -m reactor_agent
│   └── test_*.py             # 195 项离线测试
├── docs/
│   ├── TOOL_REFERENCE.md         # 工具层接口参考（agent 可直接读）
│   ├── REMOTE_VALIDATION.md      # 远程验收步骤与判读
│   ├── GWOA_MIGRATION.md         # 智能体设计的迁移来源
│   └── AGENT_IMPLEMENTATION_PLAN.md  # Agent 搭建规划（规划时的检查记录）
├── scripts/
│   ├── build_release.py      # 生成允许清单内的远程测试 ZIP
│   ├── probe_llm.py          # 探测模型端点能力
│   ├── bench_llm.py          # 模型准确率与速度基准
│   ├── pick_model.py         # 批量模型选型（TR_MODELS=a,b,c）
│   ├── probe_feeds_add.py    # Feeds.Add 失败定位探针
│   └── run-all-tests.cmd     # 一键跑全部离线测试
├── tool-layer-runs/          # 运行产物与验收证据
├── baseline_expected.json    # 历史真机结果，用于容差比对（不是本版输出）
├── RELEASE_MANIFEST.json     # 发布包逐文件哈希
├── PROJECT_PLAN.md           # 项目计划与当前进度
├── TECH_STACK.md             # 技术栈决策与理由
├── requirements.txt
├── README.md
└── Run-*.cmd                 # 双击入口：真机验收 / 仅离线检查
```

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
python -m hysys_tools.selfcheck                              # 110 项自检
python -m unittest hysys_tools.test_reliability              # 34 项回归
python -m unittest reactor_agent.test_selection              # 选型规则
python -m unittest reactor_agent.test_compiler               # 规格编译
python -m hysys_tools --list-capabilities                    # 能力表（JSON）
python -m hysys_tools --validate-only --spec specs\case.json # 离线预检
python -m hysys_tools.remote_check --offline                 # 离线验收（产出证据 ZIP）
```

一键跑全部：`scripts\run-all-tests.cmd`

## 用自然语言驱动（Agent 层）

```bash
# 凭据只从环境变量读，不写入任何文件
# PowerShell:  $env:TR_KEY = "<key>"

python -m reactor_agent --list-scenarios          # 三个内置场景
python -m reactor_agent --scenario toluene        # 默认 dry run：不启动任何模拟
python -m reactor_agent --scenario toluene --execute    # 真的驱动 HYSYS
python -m reactor_agent --text "甲苯进料10000kg/h，380℃，2.5MPa，转化率50%"
python -m reactor_agent --file request.txt --basis mass_fraction
python -m reactor_agent --graph --scenario smr    # 走状态图，可暂停追问
```

**默认不执行任何模拟**（dry run）：它会真实地抽取、归一化、选型、编译并预检，
然后打印"本来会运行"的规格。这也是本机（无 HYSYS）验证前半段的方式。

**暂停与恢复**：当请求信息不足时，`--graph` 会暂停并列出问题，**此时没有创建任何
案例**；用 `--answer` 作答后从检查点继续：

```bash
python -m reactor_agent --graph --scenario gasification
#   PAUSED for clarification. Nothing was executed.
#   ? 进料流量 80000 Nm3/h 指的是哪一股物流…
#     id: q-volumetric-flow
python -m reactor_agent --graph --scenario gasification \
       --answer 'q-volumetric-flow=80000 Nm3/h'
```

**在装有 HYSYS 的工作站上**：

1. 启动 HYSYS，处理掉弹窗，并关闭遗留的案例。
2. 在**当前窗口**设置凭据：`set TR_KEY=<your key>`（只从环境变量读，不落盘）。
3. 双击 `Run-Agent-On-Workstation.cmd` —— 它会先 dry run 气化（应拒绝），
   再真机跑甲苯与重整（两个工况），最后确认气化仍未创建任何案例。
4. 回传它打印的 `agent-runs\workstation-*\` 整个目录。

只验收工具层（不经过 Agent）则用 `Run-Remote-Validation.cmd`。

判读标准见 [docs/REMOTE_VALIDATION.md](docs/REMOTE_VALIDATION.md)。

**运行单个规格**：

```bash
python -m hysys_tools --spec specs\case.json --folder runs\unique-run-id
```

> 每次必须使用**全新的输出目录**。同一 HYSYS 实例只允许串行调用。
> 工具层只会修改和关闭**本次自己创建的**案例，不会通过活动文档定位或改动用户原有的案例。

## 能力范围与边界

已真机验证：Conversion 单反应绝热、Gibbs 等温气相体系。

**不支持**（工具层会明确拒绝，不会静默降级）：

| 组合 | 原因 |
|---|---|
| Equilibrium | 工作站上 Ln(K) 源无法设为 Gibbs（恒为 FixedK=2），拒绝而非悄悄用固定 K |
| Gibbs + 绝热 | 执行器对该组合不设热边界，从未验证 |
| 多个 Conversion 反应 | 独立校验无法区分各反应各自的转化率，会把总转化率冒充成单反应转化率 |
| CSTR / PFR | 没有实现。三个场景都未提供动力学参数，属于已声明的能力边界 |

**实验性**（代码路径存在，但没有通过验收的真机记录）：Conversion 等温、
转化率系数、涉及固体碳的 Gibbs（气化）。

**已知物理限制**：HYSYS 组分 Gibbs 数据温区上限读回为 426.85 K，而重整工况需要
873–983 K（600/710°C），因此平衡常数可能是**外推值**。工具层在结果中输出该
warning，报告不得隐瞒。

**气化场景的输入保护**：题目给的流量是 `80000 Nm3/h`，但煤是固体、水是液体，
Nm³ 只对气体有意义，且未说明标况与所指流股。工具层因此**拒绝执行并列出待澄清
问题**，不自行选定一种解释——不同解释会给出完全不同的 CO 收率。这是输入保护，
不代表气化模拟已通过。

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
  —— 交接检查前请先读它：里面有**看起来像 bug 但属于设计意图**的十条陷阱
  （气化被拒绝、`experimental` 的含义、为什么不改 `hysys_tools/`）、分钟级验证步骤，
  以及已声明的局限
- **项目计划与当前进度**：[PROJECT_PLAN.md](PROJECT_PLAN.md)
- **技术栈决策与理由**：[TECH_STACK.md](TECH_STACK.md)
- 工具层接口与错误处理：[docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)
- 远程验收流程：[docs/REMOTE_VALIDATION.md](docs/REMOTE_VALIDATION.md)
- **智能体设计的迁移来源**：[docs/GWOA_MIGRATION.md](docs/GWOA_MIGRATION.md)
- Agent 搭建规划（**规划时的历史记录，不代表当前状态**）：[docs/AGENT_IMPLEMENTATION_PLAN.md](docs/AGENT_IMPLEMENTATION_PLAN.md)

`baseline_expected.json` 来自历史真机结果，用于容差比对，**不是本版计算输出**。
证据 ZIP 会保存本次代码哈希、spec、日志、结果以及成功保存的 HSC。
