# 智能体层修复实施计划（交给本地 Agent 执行）

2026-10-03

## 背景、目标与交付物

本计划把 `reactor_agent/` 同步到已验收的工具层 `2026-10-03-equilibrium-integration-1`，并修复审查发现的智能体缺陷；`hysys_tools/` 一个字节都不改。按步骤 0 到 13 顺序执行，每步结束跑测试并提交。步骤 13（网页界面）在步骤 12 和检查点 C 之后再做。

**输入**

- `hysys-agent.zip`：完整仓库，下文所有路径都相对它的根目录。
- `hysys-tool-layer.zip`：已验收工具层。其中 `MANIFEST.json` 记录验收 `20261003-105341-4467d9c9`，状态为 `ALL_SCENARIOS_PASS`，并列出各文件的 SHA256。仓库里 `hysys_tools/` 的 13 个运行文件已核对，与它逐字节一致。

**要达到的行为**

| 场景 | 选型 | 能力状态 | 追问 |
| --- | --- | --- | --- |
| 甲苯歧化 | Conversion，绝热 | verified | 无 |
| 甲烷蒸汽重整 600/710°C | Equilibrium，等温气相，两个工况 | verified | 无；流量按甲烷 1000 kmol/h 自定，记为假设 |
| 水煤浆气化 1400°C | Gibbs + `solid_carbon=saturation` | verified | Nm³ 标准状态、煤按纯碳，两题都带默认答案 |

**修复的问题**

| 问题 | 现象 | 所在步骤 |
| --- | --- | --- |
| 能力表落后于工具层 | Equilibrium 被判 unsupported，气化被判 experimental | 2 |
| 重整选 Gibbs | 没有用上已验收的 Equilibrium | 3 |
| 热边界两处不一致 | 甲苯显示 experimental，实际按绝热执行 | 3、6 |
| “不可逆”被判为可逆 | 字符串包含“可逆” | 3 |
| “请用 Gibbs 反应器”被当成平衡数据 | 关键词 `gibbs` 太宽 | 3 |
| Nm³ 无法继续 | 只有阻塞问题，回答后仍被再问 | 4、5、7 |
| 追问死循环 | normalize 与 compiler 对同一字段各提一题，后者 id 不可路由 | 5、7 |
| 问题 id 跨进程变化 | 用了 `hash()`，`--answer` 分两次运行时对不上 | 4 |
| 自定流量锚错组分 | 锚到比例最大的水，甲烷只有 370 kmol/h | 4 |
| 二甲苯等分未声明 | 只写进 applied 日志，没有 Assumption | 4 |
| Gibbs 候选产物不全 | 气化缺 CO2/CH4，饱和碳路径要求齐全 | 6 |
| 反应名与案例名 | 中文反应名送进 HYSYS；两工况案例名相同 | 5 |
| 甲苯反应相态写死 liquid | 应为 combined，与已验收 spec 一致 | 5、10 |
| 编译失败状态错误 | `CompileError` 后状态停在 WAITING_INPUT 且无问题 | 6 |
| 两套解释文字 | pipeline 与 explain 节点各写一套，内容不一致 | 8 |
| 结果字段丢失 | Q/K、饱和碳、热负荷口径、warnings 未透传 | 8 |
| 方程式误判 | `\d\s*[A-Z]` 命中 H2O、2.5MPa，误报系数无依据 | 9 |
| CLI 不便演示 | 默认单遍路径，`--answer` 必须带 `--out` | 10 |

**交付物**

- 修改后的 `reactor_agent/`，以及新增的 `reactor_agent/report.py`。
- 更新后的测试，Agent 层与工具层全部通过。
- 更新后的 `README.md`、`Run-Agent-On-Workstation.cmd`，以及新增的 `docs/tool-acceptance-20261003-105341.json`。
- 本机中文网页界面 `python -m reactor_agent.web`（步骤 13），凭据在页面上运行时填写。
- 一个用 `scripts/build_submission.py` 打出的提交包。

**不在范围内**：修改 `hysys_tools/`、实现 CSTR/PFR、改动模型提示词与 `llm.py`。

## 执行规则（本地 Agent 必须遵守）

这些规则优先于下面任何一步的细节；冲突时停下来问人，不要自行取舍。

1. **不改 `hysys_tools/`。** 它已通过真机验收，任何改动都会让验收失效，也会让步骤 2 的能力表自动降级。需要工具层的常量或函数时只导入，不复制、不修改。
2. **不编造数值。** 默认值只能出现在问题的 `default` 字段里，或者记录成 `Assumption`，并在报告中列出。不能悄悄写进 spec。
3. **一步一提交。** 每完成一个步骤，跑一次 Agent 层全部测试，通过后提交，提交信息写 `agent-fix: step N <摘要>`。测试失败就停在这一步修，不要带着失败进入下一步。
4. **旧测试只在结论确实变了时才改。** 步骤 11 列出了允许修改的旧测试。其余测试一旦失败，说明改坏了，应修代码，不能改测试。
5. **报告文字只用中文，状态码和字段名保持英文。** 例如 `WAITING_INPUT`、`flow_input`。
6. **控制台输出要兼容 Windows。** 沿用 `_print_ascii` 和 `_use_utf8_console`，不能因编码问题抛异常。
7. **新代码不得依赖新的第三方库。** 只能用标准库、`pydantic` 和 `langgraph`，与现有 `requirements.txt` 一致。
8. **不确定就留问题，不猜。** 遇到计划没覆盖的情况，在 `docs/AGENT_FIX_NOTES.md` 里记一条，写明做了什么选择和原因。

## 步骤 0：环境准备与基线

目标是确认起点干净：工具层与验收版本一致，现有测试全部通过。基线不过就不要开始改。

1. 把 `hysys-agent.zip` 解压到一个**新目录**，例如 `D:\BiShi\hysys-agent-fix`，不要覆盖原仓库。在新目录执行 `git init`，把全部文件提交为 `baseline`。
2. 激活已有环境：`conda activate hysys-agent`，确认 `python --version` 为 3.12。
3. 把 `hysys-tool-layer.zip` 里的 `MANIFEST.json` 复制为 `docs/tool-acceptance-20261003-105341.json`。
4. 用下面的脚本核对工具层哈希，13 个运行文件都应输出 `OK`：

```python
import hashlib, json, pathlib
m = json.load(open('docs/tool-acceptance-20261003-105341.json', encoding='utf-8-sig'))
for rel, want in m['sha256'].items():
    if not rel.startswith('hysys_tools/'):
        continue
    got = hashlib.sha256(pathlib.Path(rel).read_bytes()).hexdigest()
    print(rel, 'OK' if got == want else 'MISMATCH')
```

5. 跑基线，预期数量如下，全部为 OK：

| 命令 | 预期 |
| --- | --- |
| `python -m unittest discover -s reactor_agent -t .` | 242 项通过 |
| `python -m unittest discover -s hysys_tools -t .` | 184 项通过 |
| `python -m hysys_tools.selfcheck` | 退出码 0 |
| `python scripts\test_build_submission.py` | 退出码 0 |

6. 如果工作站上还留着验收 `20261003-105341-4467d9c9` 的证据 ZIP，把它复制到 `tool-layer-runs/`。步骤 2 会引用这个路径；文件缺失不影响代码，但报告的证据链会少一环。

**完成标志**：哈希全部 OK，基线测试全部通过，`docs/tool-acceptance-20261003-105341.json` 已提交。

## 步骤 1：`reactor_agent/schemas.py` 数据契约

给进料加上工具层已验收的 Nm³ 路径字段，给问题加上“建议答案”。只加字段，不删字段，现有测试应全部保持通过。

**1.1 `FeedSpec` 新增三个字段**，与工具层 spec 的同名键一一对应：

```python
flow_input: Literal['local', 'normal_volume'] = 'local'
standard_temperature_C: float | None = None
standard_pressure_kPa: float | None = None
```

**1.2 在 `FeedSpec` 加一个 `model_validator(mode='after')`**，只在 `flow_input == 'normal_volume'` 时生效：

- `total_flow` 不能为 None。
- `total_flow_unit.strip().casefold()` 必须在 `hysys_tools.core.NORMAL_VOLUME_UNITS` 里（即 Nm3/h 的几种写法）。
- `standard_temperature_C` 与 `standard_pressure_kPa` 都必须给出；压力大于 0，温度高于 -273.15。
- `basis` 只能是 `molar_fraction` 或 `mass_fraction`。Nm³ 是总量，按组分给流量没有意义。

校验失败抛 `ValueError`，pydantic 会转成 `ValidationError`，与现有 `_unit_matches_basis` 的做法一致。

**1.3 `Question` 新增字段**：

```python
# 建议答案。只在用户确认（终端回车或 --accept-defaults）后才生效，
# 不得由代码直接写进 spec 或 facts。
default: str | None = None
```

`default` 的文字必须是 `nodes/answers.py` 能解析的回答本身。例如 Nm³ 题的默认值写成 `总进料，0°C/101.325 kPa`，煤的题写成 `按纯碳处理`。这样“回车采用默认”就等于用户输入了这段文字，不需要单独的代码路径。

**1.4 不改的地方**：`ProcessRequest`、`Assumption`、`ModelingPlan` 保持原样。固体碳仍由 `ProcessRequest.has_solid_reactant` 表示。

**新增测试**（放进 `test_compiler.py` 的 `ContractGuards`）：

- `normal_volume` 配 `kg/h` 抛 `ValidationError`。
- `normal_volume` 缺 `standard_pressure_kPa` 抛 `ValidationError`。
- `normal_volume` + `Nm3/h` + `0.0` + `101.325` + `mass_fraction` 可以构造。
- `Question(default='x').model_dump()` 含 `default`。

## 步骤 2：`reactor_agent/capabilities.py` 能力表重写

验收记录写在智能体这边，与工具层的版本号和文件哈希绑定。哈希全部一致时，按验收结果给出 verified；任何一个文件变了，所有 verified 自动降为 experimental，并写明原因。工具层自己的 `capabilities.py` 仍写着 pending，那是它打包前的文字，**不要去改它**。

**2.1 模块级常量**（哈希取自 `MANIFEST.json`，原样复制）：

```python
ACCEPTED_TOOL_REVISION = '2026-10-03-equilibrium-integration-1'
ACCEPTED_RUN = '20261003-105341-4467d9c9'
ACCEPTED_STATUS = 'ALL_SCENARIOS_PASS'
ACCEPTANCE_RECORD = 'docs/tool-acceptance-20261003-105341.json'
ACCEPTANCE_EVIDENCE = 'tool-layer-runs/acceptance-20261003-105341-4467d9c9'
ACCEPTED_SHA256 = {
    '__init__.py': 'b2366bc487d0d94afee96598fa2b621cf884a8101bd0619d346dca477ef389a1',
    '__main__.py': '93d83f4af888721746b822c3e064d71ec8531308d796eac92ab8ea8d27e24679',
    'capabilities.py': '259c9e94d5123a39ac47f35afb693efc10cc7167716bf53304fc4f8dfe8aa35e',
    'core.py': 'e23f151968c18ca2b851cc9f4b8c9bde76ab1cd6c4c3e7d6f07a1ba1f6946e88',
    'equilibrium.py': 'cbe3089a0849a939bf547557a1aa85ed990f10783a368d5139a97f9a2d5c607c',
    'examples.py': 'b2077d7b1acbf38089fd6b95a87c565c971c83eb3945668511cc34d003f50605',
    'main.py': 'd0ab80500be3b4040f473a0197aa188cc69bbd798d12500423e5791e2204ea2f',
    'native_flow.py': '5e759b63f50348c829d66036aab04308f72a8e4e2688dfd287256553731b8c62',
    'precheck.py': '1da6dc12f8406c316e0ea2286a375d5b3dd86f42a6fc7e084a5a1616b586fe69',
    'reactor.py': '604a3a9ead0c36992cb45df74aa5926132e8982d176140920f9d9be1258dcb9a',
    'saturation.py': '332ce5ad9a9a8d34dded8b4298d44b4e53fc3b12d82134ea0aa6dfe3ac8fc675',
    'thermo_reference.py': '7bd6c4e98126276ef7218c348094a9c57cf282940ed5dde7201dfb5fc842aa5b',
    'validate.py': '8ca7608e6700c5d1bac8b7847a8e16dce2411b945602d59c6f9f60b49a67ae49',
}
```

**2.2 `acceptance_state() -> dict`**，用 `functools.lru_cache` 缓存：

- 读 `hysys_tools.capabilities.TOOL_REVISION`，与 `ACCEPTED_TOOL_REVISION` 比较。
- 对 `Path(hysys_tools.__file__).parent` 下每个文件计算 SHA256，与表比较。文件缺失算不一致。
- 返回 `{'holds': bool, 'tool_revision': ..., 'mismatched': [文件名...], 'reason': 中文说明}`。
- 把哈希计算放进单独函数 `_file_sha256(path)`，测试可以用 `unittest.mock.patch` 替换它，再调用 `acceptance_state.cache_clear()`。

**2.3 `combination_status()` 新签名**，新增参数都带默认值，旧调用不受影响：

```python
def combination_status(reactor_kind, thermal_mode=None, phase=None,
                       reaction_count=1, solid_phase=False) -> dict
```

返回结构保持 `{'status', 'reason', 'evidence', 'rule'}`。判定表如下，标 ★ 的行只有在 `acceptance_state()['holds']` 为真时才是 verified，否则降为 experimental，`reason` 前加上不一致的说明，`evidence` 置空。

| kind | 条件 | status | rule |
| --- | --- | --- | --- |
| cstr / pfr | 任意 | unsupported | `no_implementation` |
| conversion | `reaction_count > 1` | unsupported | `multiple_conversion_reactions` |
| conversion | adiabatic | verified ★ | `conversion_adiabatic_single` |
| conversion | isothermal | experimental | `conversion_isothermal_single` |
| conversion | 热边界为 None | experimental | `conversion_unspecified_thermal` |
| equilibrium | `solid_phase` | unsupported | `equilibrium_solid` |
| equilibrium | `phase == 'liquid'` | unsupported | `equilibrium_liquid` |
| equilibrium | 非 isothermal（含 None） | unsupported | `equilibrium_needs_isothermal` |
| equilibrium | isothermal | verified ★ | `equilibrium_isothermal_vapour` |
| gibbs | adiabatic | unsupported | `gibbs_adiabatic_unimplemented` |
| gibbs | 热边界为 None | experimental | `gibbs_unspecified_thermal` |
| gibbs | isothermal 且 `solid_phase` | verified ★ | `gibbs_isothermal_solid_saturation` |
| gibbs | isothermal | verified ★ | `gibbs_isothermal_gas` |

`gibbs_isothermal_solid_saturation` 的 `reason` 必须写明三点：只验收了碳加水进料；走的是 `solid_carbon=saturation` 组合流程，不是直接用库 Carbon 的单台 Gibbs；未反应碳会出现在名为 LIQUID 的物流里。

★ 行的 `evidence` 写成 `['%s（验收 %s，%s）' % (ACCEPTANCE_RECORD, ACCEPTED_RUN, ACCEPTED_STATUS), ACCEPTANCE_EVIDENCE]`。conversion 与 gibbs 气相两行再保留原来的历史证据路径 `tool-layer-runs/20261002-111439/...`。

**2.4 `capability_report()`**：保留对 `hysys_tools.capabilities.capability_metadata()` 的读取，把 `agent_notes` 改成新的验收信息（版本、run id、状态、`acceptance_state()` 结果），并更新“验收未覆盖”的清单：等温 Conversion、多反应 Conversion、绝热 Gibbs 与绝热 Equilibrium、液相或含固体的 Equilibrium、碳加水以外的气化进料、CSTR/PFR。

**2.5 `is_executable()`** 透传新参数，逻辑不变。

## 步骤 3：`reactor_agent/selection.py` 选型规则

加入“反应网络闭合 → Equilibrium”规则，让重整选到已验收的 Equilibrium；统一热边界；修正两个关键词误判；提供 Gibbs 候选产物补齐函数供步骤 6 使用。

**3.1 统一热边界：新增 `planned_thermal_mode(request) -> str`**

- 各工况显式给出的 `thermal_mode` 只有一种时，用它。
- 否则，有工况且每个工况都有出口温度时，返回 `'isothermal'`。
- 其余情况返回 `'adiabatic'`。

`select_reactor` 里所有 `capability(kind, ...)` 调用改用这个函数，替换 `_thermal_from_cases`。仓库里没有其他地方引用 `_thermal_from_cases`，可以删掉，或保留成调用新函数的一行包装。这样甲苯（无出口温度）在选型时就按绝热查表，得到 verified，与执行时一致。

**3.2 修正可逆判断**。`is_reversible_declared` 改用正则，避免“不可逆”“irreversible”被当成可逆：

```python
_REVERSIBLE = re.compile(r'(?<!不)可逆|(?<!ir)reversible|⇌|⇄|<=>|<->', re.I)
_IRREVERSIBLE = re.compile(r'不可逆|irreversible', re.I)
```

判定：任一反应 `reversible is True`，或 `_REVERSIBLE.search(source_text)` 命中。

**3.3 修正平衡数据判断**。`_has_equilibrium_data` 不再把单独的 `gibbs`、`ka` 当证据，只认真正的平衡数据：

```python
_EQUILIBRIUM_DATA = re.compile(
    r'平衡常数|equilibrium\s+constant'
    r'|(?<![A-Za-z])ln\s*\(?\s*k(?![A-Za-z])'
    r'|(?<![A-Za-z])k[pca]?\s*[=＝:：]\s*[-+]?\d'
    r'|[Δδ]\s*G'
    r'|吉布斯自由能(?:变|数据)|标准生成(?:吉布斯)?自由能'
    r'|gibbs\s+(?:free\s+)?energy\s+(?:data|change|of\s+reaction)', re.I)
```

注意中文字符在 Python 正则里属于 `\w`，所以用 `(?<![A-Za-z])` 代替 `\b`。

**3.4 新规则：反应网络闭合 → Equilibrium**

新增常量与函数：

```python
RULE_EQUILIBRIUM_NETWORK = 'equilibrium_closed_network'
BLACK_BOX_TOKENS = ('气化', '裂解', '黑箱', '燃烧', '机理复杂', '机理未知', '产物未知',
                    'gasif', 'crack', 'black box', 'combustion',
                    'unknown mechanism', 'unknown product')
_ASKS_FOR_GIBBS = re.compile(r'gibbs\s*(?:反应器|reactor|模型)|吉布斯反应器|自由能最小', re.I)
```

`reaction_network_is_closed(request) -> bool`，以下全部成立才返回 True：

1. 至少一个反应，且 `request.has_solid_reactant` 为 False。
2. 反应中每个组分都能用 `hysys_tools.core.library_name` 解析，且都不是 Carbon。
3. `request.components` 里的每个组分，要么出现在某个反应里，要么出现在进料里（惰性组分）。不满足说明有反应式之外的副产物，应交给 Gibbs。

`is_equilibrium_candidate(request) -> bool`，以下全部成立才返回 True：

1. 没有动力学参数，没有转化率约束。
2. `reaction_network_is_closed(request)`。
3. 至少一个工况，且每个工况都有出口温度。
4. 原文不含 `BLACK_BOX_TOKENS`，`_IRREVERSIBLE` 不命中，`_ASKS_FOR_GIBBS` 不命中。

注意：不要用 `ReactionSpec.reversible` 字段排除，它是模型的判断，不是用户的原话。也不要把“副反应”当成黑箱词，重整原文就写了“副反应”，它是闭合网络的一部分。

在 `select_reactor` 中，把新分支放在第 4 步（可逆加平衡数据）之后、第 5 步（Gibbs）之前：

- `preferred_reactor='equilibrium'`，`rule_id=RULE_EQUILIBRIUM_NETWORK`，`alternatives=['gibbs']`。
- 能力查 `capability('equilibrium', planned_thermal_mode(request))`。
- `evidence` 列出每个反应的 `equation_text(...)`，以及“列出的组分都在反应式内”“无动力学、无转化率”“出口温度给定”。
- `explanation` 用中文写明：用户写出了 N 个反应（逐条列出方程），反应网络闭合；平衡常数由 HYSYS 组分 Gibbs 数据按出口温度拟合，工具层会校验 Q/K；Gibbs 反应器保留为对照方案。

同时修两处已有分支：第 3 步“只有收率”的豁免条件加上 `or is_equilibrium_candidate(request)`；第 4 步可执行时 `fallback_reason` 应为 None，现在写的是“工作站无法设置 Ln(K) 源”，已经过时。

**3.5 新增 `complete_gibbs_candidates(components) -> tuple[list[str], list[str]]`**

返回补齐后的组分表和新增的组分。按固定顺序检查 `('CO', 'CO2', 'Hydrogen', 'Water', 'Methane')`：某个候选的元素（用 `hysys_tools.core.atoms_of`）全部包含在现有组分的元素集合里，且尚未列出，就追加。气化的 `[Carbon, Water, CO, Hydrogen]` 会补上 `CO2` 和 `Methane`；重整的五组分不变。解析不了元素的组分直接跳过，不抛异常。

**新增测试**（`test_selection.py`）：

- 重整（两个反应、五个组分、600/710°C 两个工况）选 `equilibrium`，规则为 `RULE_EQUILIBRIUM_NETWORK`，状态 verified，`execution_reactor == 'equilibrium'`。
- 同一请求原文加上“请用 Gibbs 反应器”，选 `gibbs`。
- 去掉 WGS 反应但保留 CO2，不再闭合，选 `gibbs`。
- 气化（反应含 Carbon、1400°C）选 `gibbs`，状态 verified。
- `is_reversible_declared`：“不可逆”“irreversible”为 False；“可逆”“reversible”“⇌”为 True。
- `_has_equilibrium_data`：“请用 Gibbs 反应器”为 False；“Kp = 2.3”“平衡常数”“ΔG = -20 kJ/mol”为 True。
- 不带工况的甲苯请求，`capability_status == 'verified'`。
- `complete_gibbs_candidates(['Carbon', 'Water', 'CO', 'Hydrogen'])` 新增 `['CO2', 'Methane']`。

## 步骤 4：`reactor_agent/normalize.py` 归一化

让 Nm³ 有一条“确认后可执行”的路径，修正自定流量的锚定组分，把二甲苯等分记成假设，问题 id 改成跨进程稳定。原则不变：没有用户确认，Nm³ 就不换算。

**4.1 Nm³/h：带默认答案的追问，确认后走 `normal_volume`**

现在遇到体积单位只会提阻塞问题。改为两种情况：

- **facts 里没有 `normal_volume_basis`**（用户还没确认）。照旧把单位原样保留，`report.record(...)` 里保留 `NOT converted` 字样（已有测试依赖它），并提问：
    - `id='q-volumetric-flow'`，`field='feeds[0].total_flow_unit'`，`blocking=True`。
    - `question`：`进料流量 80000 Nm3/h 按什么理解？默认：单股混合进料的总量，标准状态 0°C / 101.325 kPa。也可以回答其他标准状态（如 20°C），或直接给出质量或摩尔流量（如 49086 kg/h）。` 其中数值和单位来自 facts，必须保留数值本身（测试检查 `80000` 在问题里）。
    - `default='总进料，0°C/101.325 kPa'`，只在单位属于 `hysys_tools.core.NORMAL_VOLUME_UNITS` 时给。普通 `m3/h`（工况体积）不给默认值，问题改为请用户给出质量或摩尔流量。
- **facts 里有 `normal_volume_basis`**，值由步骤 7 写入，形如 `{'standard_temperature_C': 0.0, 'standard_pressure_kPa': 101.325}`，且单位是 Nm³。此时：
    - `FeedSpec` 设 `flow_input='normal_volume'`，并填入两个标准状态字段，单位规范成 `Nm3/h`。
    - 追加 `Assumption(id='a-normal-volume', field='feeds[0].total_flow', source='user_answer', accepted=True)`。`value` 写 `'80000 Nm3/h @ 0°C/101.325 kPa'`；`scope` 写明：这是单股混合进料的总量；按理想气体摩尔体积换算，0°C/101.325 kPa 时为 22.414 m³/kmol，即 R(T+273.15)/P；由工具层换算成摩尔流量交给 HYSYS，不用 HYSYS 自带的 15°C 标准体积。
    - `report.record` 记一条 `normal volume confirmed by the user ...`，不要再写 `NOT converted`。

**4.2 自定流量锚定到含碳反应物**

替换现在“取比例最大的组分”的逻辑，新增 `_flow_anchor(fractions, reactions) -> str`，按顺序选：

1. 在某个反应中系数为负、且含碳（`'C' in atoms_of(name)`）的进料组分，取比例最大的一个。
2. 没有反应时，取含碳的进料组分中比例最大的一个。
3. 都没有时，退回比例最大的组分。

总流量为 `DEFAULT_PRINCIPAL_KMOL_H / fractions[anchor]`。重整（1:2.7）锚定甲烷，总流量 3700 kmol/h，即甲烷 1000、水 2700，与已验收 spec 一致。新增常量 `OPERATING_HOURS_PER_YEAR = 8000`，年处理量为 `1000 × molar_mass_of(anchor) × 8000 / 1e6`，甲烷约 128.3 kt/a。

`a-feed-flow` 的 `scope` 改成中文，例如：`题目说明进料流量可以自定、未给数值；取甲烷 1000 kmol/h（总进料 3700 kmol/h，保持 1:2.7），按 8000 h/a 计约 128.3 kt/a 甲烷，属于中型制氢装置的处理量`。`report.record` 里保留 `chosen by us` 字样（已有测试依赖它）。

**4.3 二甲苯等分记成假设**

两种来源都要记：

- `_stoichiometry` 把“二甲苯”“C8H10”展开成三个异构体时（现有逻辑）。
- 模型已经自己拆成三个异构体、系数两两相等（误差 1e-9）时。冒烟记录显示模型实际就是这样输出的，现有代码在这种情况下什么都不记。

统一追加一条（同一请求只加一次）：`Assumption(id='a-isomer-split', field='reactions.stoichiometry', value='o/m/p-Xylene 各 1/3', source='agent_default', accepted=False, scope='题目列出邻、间、对三种二甲苯但未给比例，按等分处理；这不是工业选择性，也不是热力学预测')`。

**4.4 问题 id 改用 crc32**

新增 `_stable_id(text: str) -> str`，返回 `'%08x' % zlib.crc32(text.encode('utf-8'))`。替换三处 `abs(hash(...)) % 100000`，分别在第 208、285、549 行附近，对应 `q-composition-species-`、`q-reaction-species-`、`q-component-`。原因：`hash()` 受 `PYTHONHASHSEED` 影响，暂停和 `--answer` 恢复是两个进程，id 会变，用户的回答就对不上问题。

**4.5 更新过时文字**：`a-solid-phase` 的 `scope` 改为“进料含参与反应的固体碳，按 solid_carbon=saturation 组合流程执行（已随工具层验收）”。模块文档字符串里“Nm3/h 一律不换算”的说法，改成“未经用户确认不换算”。

**新增测试**（`test_normalize.py`）：

- 未确认时：`q-volumetric-flow` 的 `default` 是 `总进料，0°C/101.325 kPa`，`flow_input` 仍为 `local`。
- facts 带 `normal_volume_basis` 时：`flow_input=='normal_volume'`、标准状态为 0 与 101.325、不再有 `q-volumetric-flow`、有 `a-normal-volume`。
- `m3/h` 的问题没有 `default`。
- 重整自定流量：总流量 3700、甲烷份额乘总流量等于 1000、`scope` 含 `128`。
- 甲苯 facts（三个异构体各 1/3）产生 `a-isomer-split`。
- `_stable_id('未知物质')` 在两个子进程里结果相同：用 `subprocess` 分别以 `PYTHONHASHSEED=1` 和 `2` 运行并比较输出。

## 步骤 5：`reactor_agent/compiler.py` 编译器

按反应器类型输出正确的反应块，透传 Nm³ 和饱和碳字段，生成 ASCII 的反应名和案例名，并消除重复追问。编译结果要能通过工具层预检，关键字段与已验收 spec 一致。

**5.1 反应块按反应器类型生成**。把 `_reaction_entry(request, index)` 改成 `_reaction_entry(request, index, kind)`，并替换 `compile_case` 里只给 conversion 输出反应的那一行：

| 反应器 | 输出的 `reactions` | `phase` | 其他字段 |
| --- | --- | --- | --- |
| conversion | 全部（通常一个） | 固定为 `'combined'` | `conversion_percent`、`base_component` |
| equilibrium | 全部 | 固定为 `'vapour'` | 不带转化率字段 |
| gibbs | `[]` | — | — |

删掉现在按 `request.phase` 映射 liquid/vapour/combined 的逻辑。已验收的甲苯 spec 用 `combined`，Equilibrium 工具层只接受 `vapour`。

**5.2 反应名统一为 ASCII**：`'name': 'RXN-%d' % (index + 1)`。转化率约束仍按请求里的原始反应名匹配（`constraint.reaction == reaction.name`），只是输出时换成 `RXN-n`。原因是模型给的反应名可能是中文（冒烟记录里是“歧化反应”），会直接成为 HYSYS 里的反应对象名。

**5.3 进料透传 Nm³ 字段**。`_feed_entry` 在 `feed.flow_input == 'normal_volume'` 时追加 `flow_input`、`standard_temperature_C`、`standard_pressure_kPa` 三个键。`local` 时**不要**写 `flow_input` 键，这样甲苯和重整的 spec 与以前逐字相同，执行台账里的 spec 哈希不变。

**5.4 饱和碳**。反应器为 gibbs、且进料里有组分规范名为 `carbon`、份额大于 0 时，设 `reactor['solid_carbon'] = 'saturation'`。工具层对“普通 Gibbs + 库 Carbon”会在求解前拒绝，所以这一步必须加，不是可选项。

**5.5 案例名**。新增 `_case_name(plan, case) -> str`：

- 有 `case.overrides['case_name']` 时，原样用 `safe_case_name` 处理，不加后缀。
- 否则 `base = safe_case_name(request.scenario_label, fallback='')`。中文标签会清洗成空串，这时用 `'agent-%s' % execution_reactor`。
- 该工况有出口温度时，加后缀 `'-%gC' % outlet`，例如 `agent-equilibrium-710C`；没有温度但有多个工况时，加 `safe_case_name(case.case_id)`。

重整两个工况的案例名因此不同，且都是 ASCII。

**5.6 追问去重**。`compile_plan` 合并编译器问题时，原来按 `(id, field)` 去重，改为按 `field` 去重：同一字段已有问题，就不再追加。这正是死循环的来源：normalize 提了 `q-volumetric-flow`，编译器又对同一字段提了 `q-flow-basis-0`，而后者的 id 在回答路由里不存在，永远答不掉。

配套修改：

- `_flow_unit_is_convertible` 改成接收整个 `FeedSpec`。`flow_input == 'normal_volume'` 视为可用，不再提 `q-flow-basis-*`。
- `q-flow-basis-*` 也带上与 4.1 相同的 `default`，因为测试会直接调用 `feed_questions`。
- 煤的问题 `field` 从 `'feeds[0].fractions'` 改为 `'feeds[0].coal_definition'`，避免与“缺进料组成”的问题撞字段而被去重吞掉。

**5.7 煤的问题带默认答案**：

- `question`：`煤能否按纯固体碳处理？默认：按纯碳处理。说明：工具层目前只验收了“碳 + 水”进料，按元素分析建模需要另行扩展。`
- `default='按纯碳处理'`。

新增 `coal_assumption(request) -> Assumption | None`。原文提到煤、且已确认按纯碳（含 `COAL_CONFIRMATION` 文字或原有的纯碳关键词）时，返回 `Assumption(id='a-coal-pure-carbon', field='feeds[0].fractions', source=...)`：确认来自追问时 `source='user_answer'`，来自原文时为 `'user_text'`；`scope` 为“煤按纯固体碳处理；题目只说忽略灰分，这一项是建模假设，会影响碳平衡和 CO 收率的分母”。步骤 6 调用它。

**5.8 spec 里的假设文字**。`spec['assumptions']` 改为 `'%s：%s' % (a.field, a.scope or a.value)`，让工具层 `result.json` 里的假设可读。

**新增与修改的测试**（`test_compiler.py`）：

- `ReformerCompilation.test_gibbs_spec_carries_no_reactions` 改名为 `test_equilibrium_spec_carries_vapour_reactions`：两个反应，名为 `RXN-1`、`RXN-2`，`phase` 都是 `vapour`，`reactor.kind == 'equilibrium'`，预检通过。
- 新增 Gibbs 对照：原文加“请用 Gibbs 反应器”后 `reactions == []`，`reactor.kind == 'gibbs'`。
- 重整两个 spec 的 `case_name` 不同，分别以 `-710C`、`-600C` 结尾，且 `case_name.isascii()`。
- 甲苯 spec：`reactions[0]['phase'] == 'combined'`，`name == 'RXN-1'`，没有 `flow_input` 键。
- 气化确认后（`flow_input='normal_volume'`，原文含“按纯碳”）：状态 READY，`reactor.solid_carbon == 'saturation'`，预检通过，`readback['normal_volume_conversion']['molar_flow_kmol_h']` 约为 3569.20（`places=2`）。
- 气化未确认时：`feeds[0].total_flow_unit` 字段上只有一个问题；煤的问题带 `default`。

## 步骤 6：`pipeline.build_plan` 与 `nodes/plan.py`

`build_plan` 负责把选型结论变成完整的建模计划：补齐 Gibbs 候选产物，记录饱和碳、纯碳和平衡常数的假设。plan 节点修正编译失败时的状态。

**6.1 `build_plan(request, decision, report, heat_mode=None)`**

1. `heat_mode` 为 None 时改用 `selection.planned_thermal_mode(request)`。`a-thermal-mode` 假设的逻辑和文字保持不变（测试检查“等温”）。
2. 先 `components = list(request.components)`。`decision.execution_reactor == 'gibbs'` 时，调用 `complete_gibbs_candidates(components)`；有新增时追加：
    - `Assumption(id='a-gibbs-candidates', field='fluid_package.components', value=added, source='agent_default', accepted=False, scope='Gibbs 只在给定组分中按自由能最小分配产物；题目未逐一列出，按进料元素补齐候选产物 CO2、Methane')`，`scope` 里的组分名按实际新增的写。
3. 反应器为 gibbs 且进料含碳时（与 5.4 判断一致）：
    - 追加 `Assumption(id='a-solid-carbon-route', field='reactor.solid_carbon', value='saturation', source='derived', accepted=True)`。`scope` 写明：转化率反应器加仅含气相的 Gibbs 反应器，外层求解使气相碳活度为 1；不使用 HYSYS 库 Carbon 的 Gibbs 数据；未反应碳出现在名为 LIQUID 的物流中，实为固相。
    - 进料里没有 Oxygen 时，追加一个**非阻塞**问题 `Question(id='q-no-oxygen', field='feeds[0].oxygen', blocking=False, question='题目没有给出氧气进料，维持出口温度需要外部供热；报告的热负荷是外供热，不是自热气化。')`。它会进入 spec 的 `open_questions`，不会暂停运行。
4. 调用 `compiler.coal_assumption(request)`，不为 None 就追加。
5. 反应器为 equilibrium 时追加 `Assumption(id='a-equilibrium-k', field='reactions', source='derived', accepted=True)`。`scope` 写明：平衡常数由 HYSYS 组分 Gibbs 数据在出口温度上下 150 K 内拟合 ln K = A + B/T + C·ln T；工具层校验拟合残差与出口 Q/K；Q/K 接近 1 不能证明高温区 Gibbs 数据本身准确。
6. `ModelingPlan(components=components, ...)` 用补齐后的列表。

**6.2 `nodes/plan.py`**

- 捕获 `CompileError` 后，除了记录 `problems`，还要把 `status` 设为 `FAILED`。现在的写法是 `compiled = plan`，状态停在默认的 `WAITING_INPUT`，问题列表却是空的，用户看到的是“等待输入”却没有任何问题可答。
- `question_to_dict` 增加 `'default': question.default`。
- 写进 state 的 `assumptions` 每项增加 `'id'` 和 `'accepted'`，供步骤 8 的报告区分“我方默认”和“用户确认”。

**6.3 `run_pipeline`** 已经在 `CompileError` 时返回 FAILED，不用改。它调用 `build_plan` 的方式不变。

**新增测试**：

- `test_normalize.py` 的 `ThermalBoundaryIsDeclared` 旁新增：用气化 facts 构建的 plan，组分含 `CO2` 和 `Methane`，有 `a-gibbs-candidates`、`a-solid-carbon-route`，`plan.questions` 中有非阻塞的 `q-no-oxygen`。
- `test_graph.py` 新增：用 `unittest.mock.patch('reactor_agent.nodes.plan.compile_plan', side_effect=CompileError('boom'))` 运行，最终 `status == 'FAILED'`，`problems` 含 `compilation failed`，没有 `__interrupt__`。
- `test_graph.py` 新增：暂停时 interrupt 里的问题字典带 `default` 键。

## 步骤 7：`reactor_agent/nodes/answers.py` 回答路由

让每个会被问到的问题都有对应的处理：Nm³ 题能识别“默认”、某个标准温度、或直接改给质量/摩尔流量；编译器生成的问题 id 都能路由；煤的确认能识别否定。完成这一步后，气化应能在一轮作答后跑通，不再循环。

**7.1 新增 `parse_normal_volume_answer(answer) -> dict`**，返回三种结果之一：

| 返回 `kind` | 含义 | 其他键 |
| --- | --- | --- |
| `normal_volume` | 认可“进料总量 + 标准状态” | `standard_temperature_C`、`standard_pressure_kPa`，可选 `total` |
| `flow` | 改给质量或摩尔流量 | `value`、`unit` |
| `unreadable` | 无法识别或被否定 | `note` |

解析规则，按顺序判断：

1. dict 输入：含两个标准状态键时直接采用；含 `value`/`unit` 时按第 2、3 条处理。
2. 用现有 `parse_answer_value` 取开头的数字和单位。单位经 `normalize.FLOW_UNITS` 规范后属于 `MASS_FLOW_UNITS` 或 `MOLAR_FLOW_UNITS`，返回 `flow`。
3. 单位属于 `NORMAL_VOLUME_UNITS`（例如 `80000 Nm3/h`），返回 `normal_volume`，`total` 为该数值，标准状态取默认值。
4. 否定：去掉空白后以“不”“否”“no”“not”开头，或包含“不能”“不可以”“不行”，返回 `unreadable`。
5. 提到其他物流（“水蒸气”“蒸汽”“出口”“合成气”“syngas”）时，返回 `unreadable`，`note` 写：“目前只支持把 Nm³ 理解为进料总量；如果指其他物流，请直接给出进料的质量或摩尔流量”。
6. 用正则找温度：`(-?\d+(?:\.\d+)?)\s*(?:°\s*C|℃|摄氏度|度)`，或数字后紧跟 `C`、`K` 且后面不是字母。K 要换算成 °C。
7. 用正则找压力：`(\d+(?:\.\d+)?)\s*(kPa|MPa|Pa|bar|atm)`（忽略大小写），用 `hysys_tools.core.to_kpa` 换算。
8. 找到温度或压力时返回 `normal_volume`，缺的一项取默认值：0°C 或 101.325 kPa。
9. 都没找到、但包含“默认”“default”“总进料”“总量”“total”“是”“对”“确认”“可以”“ok”“yes”之一时，返回 `normal_volume`，两项都取默认值。
10. 其余情况（包括空字符串）返回 `unreadable`。空回答不能当成默认：采用默认值由 CLI 显式填入默认文字来完成（步骤 10）。

**7.2 `apply_answers` 里的新路由**

- `q-volumetric-flow`，以及匹配 `^q-flow-basis-\d+$` 的 id，都调用 7.1：
    - `normal_volume`：写 `merged['normal_volume_basis'] = {'standard_temperature_C': T, 'standard_pressure_kPa': P}`；有 `total` 时同时写 `feed_total` 和 `feed_unit='Nm3/h'`。
    - `flow`：写 `feed_total` 和 `feed_unit`，并删除 `normal_volume_basis`。
    - `unreadable`：不改 facts，把 `note` 追加到 `note` 列表。
- 从 `_DIRECT_IDS` 里删掉 `q-volumetric-flow`。
- 新增编译器问题的路由，用正则 `^q-(flow-missing|temp-missing|press-missing)-\d+$`，分别映射到 `('feed_total', 'feed_unit')`、`('feed_temperature', None)`、`('feed_pressure', 'feed_pressure_unit')`。
- `q-pressure-varies` 映射到 `('feed_pressure', 'feed_pressure_unit')`。

未知 id 仍然什么都不写，现有的 `test_an_unknown_question_id_writes_nothing` 必须继续通过。

**7.3 煤的确认识别否定**。`confirmation_text` 先用 7.1 第 4 条的否定规则判断，命中就原样返回文本，然后才判断肯定词。现在“不可以”因为包含“可以”会被当成同意。默认文字“按纯碳处理”包含“按纯碳”，属于肯定。

**新增测试**（`test_graph.py` 新建 `NormalVolumeAnswers` 类）：

| 回答 | 期望 |
| --- | --- |
| `总进料，0°C/101.325 kPa` | `normal_volume_basis` 为 0 与 101.325 |
| `默认` | 同上 |
| `20°C`、`20℃` | 20 与 101.325 |
| `15 C, 1 atm` | 15 与 101.325 |
| `273.15 K` | 0 与 101.325 |
| `80000 Nm3/h` | 默认标准状态，`feed_total == 80000`，`feed_unit == 'Nm3/h'` |
| `49086 kg/h` | `feed_total == 49086`，`feed_unit == 'kg/h'`，没有 `normal_volume_basis` |
| `不是`、`no idea`、空字符串 | facts 不变，`note` 非空 |
| `指出口合成气` | facts 不变，`note` 提到“其他物流” |

另外：`q-flow-basis-0` 与 `q-volumetric-flow` 结果相同；`q-temp-missing-0='40 C'` 写入 `feed_temperature == 40`；`confirmation_text` 对“不可以”不追加确认，对“按纯碳处理”追加。

**关键的端到端测试**（`test_graph.py`）：用带 Nm³ 的气化 facts 运行图，第一次暂停时问题里同时有 `q-volumetric-flow` 和 `q-coal-definition`，且都带 `default`。用两个问题的 `default` 文字恢复一次后：没有 `__interrupt__`，`status == 'READY'`（dry run），spec 里 `flow_input == 'normal_volume'`、`reactor.solid_carbon == 'saturation'`。这条测试就是死循环的回归测试。

## 步骤 8：解释层 `report.py` 与 adapter 结果透传

单遍 pipeline 和图的 explain 节点改为调用同一个报告函数，内容一致；报告里补上工具层已经算出、但现在被丢掉的检查结果，并加入两温度对比解读和 CO 收率的氧平衡上限。报告里所有数字都从结果读，不写死任何数值。

**8.1 `adapters/hysys_cli.py` 的 `ExecutionResult.results()` 增加字段**（都用 `.get` 取，缺失时为 None 或空）：

| 新键 | 来源 |
| --- | --- |
| `heat_duty_scope` | `checks['heat_duty_scope']` |
| `equilibrium_QK` | `checks['equilibrium_QK']`，含 `verdict` 和每个反应的 `Q_over_K`、`ln_Q_over_K` |
| `equilibrium_fit` | `payload['equilibrium_evidence']` 每项只取 `reaction`、`fit_max_residual`、`lnK_exact_bar`、`basis_units` |
| `solid_carbon_saturation` | `payload['solid_carbon_saturation']` 只取 `carbon_conversion_x`、`water_limited_x_max`、`carbon_activity` 里的三条 `via_*` 与 `spread_decades`、`duty_by_reactor_kW`、`library_carbon_gibbs_used` |
| `gibbs_equilibrium` | `checks['gibbs_equilibrium']` 的 `verdict`、`orders_from_equilibrium` |
| `independent_duty` | `checks['independent_duty']` 的 `verdict`、`relative_deviation` |
| `condensed_phase_location` | `checks['condensed_phase_location']['verdict']` |
| `feed_molar_flows_kmol_h` | `payload['feed_molar_flows_kmol_h']` |
| `component_flows_kmol_h` | `payload['component_flows_kmol_h']` |
| `normal_volume_conversion` | `payload['native_flow_readback']['conversion']` |

`warnings`、`assumptions`、`open_questions` 已经存在，保持不变。

**8.2 新建 `reactor_agent/report.py`**

把 `nodes/explain.py` 里的 `_fmt`、`_num`、`_percent_map`、`_outlet_of`、`_case_block`、`_comparison_table` 原样搬过来，再按下面修改。入口是：

```python
def render_report(view: dict) -> str
```

`view` 的键：`status`、`decision`（与 plan 节点的 `decision_payload` 同形）、`blocking`（问题字典列表）、`open_questions`、`assumptions`（字典列表，含 `id`、`source`）、`executions`（每项是 `summary()` 加上 `'results': results()`）、`problems`。再提供两个转换函数：`view_from_state(state)` 和 `view_from_run(run: AgentRun)`。

报告依次输出以下内容，没有内容的部分整段省略：

1. **选型**：反应器、能力状态、规则和选型理由；理论选型与执行不同时写明替换原因。状态为 `UNSUPPORTED` 时只输出这一段，并说明没有运行模拟。
2. **待确认问题**：每题一行，有默认答案的写成“（默认：…）”。
3. **假设**，分三组列出：本系统选定（`agent_default`）、用户确认（`user_answer`、`user_text`）、推导所得（`derived`）。
4. **计算结果**，每个工况一块：
    - `_outlet_of` 选主出口时跳过 Carbon 摩尔分数不低于 0.999 的物流。
    - 这类物流的标题写成“`LIQUID`（固相碳；HYSYS 物流名为 LIQUID，并非液态碳）”。
    - 热负荷后面跟中文口径：`heat_duty_scope` 以 `Adiabatic` 开头时写“绝热：热负荷为 0，出口温度为计算结果”；否则写“维持出口温度所需的外部热量，包含进料从入口温度升温的显热和反应热，不只是反应热”。
    - 有 `equilibrium_QK` 时，逐个反应写“`RXN-1`：Q/K = 1.0003，|ln(Q/K)| = 0.0003，判定 PASS”，并写出拟合最大残差。
    - 有 `co_yield` 时，在现有 CO 收率之后加三项：碳转化率（`carbon_conversion_percent`）；**氧平衡上限** = 进料 O 原子流量与 C 原子流量之比的百分数，用 `feed_molar_flows_kmol_h` 和 `hysys_tools.core.atoms_of` 计算，只在进料唯一含氧组分是水时输出（解释为“每个 CO 分子需要一个 O，氧只来自水”）；CO 收率与上限之比。比值不低于 0.9 时加一句“CO 收率主要受进料中的水量限制”。
    - 有 `solid_carbon_saturation` 时，写碳转化率 X、水量允许的上限，以及三条途径的碳活度。
    - 校验一行：元素守恒误差、质量误差、独立热负荷偏差及判定、Gibbs 平衡判定。
    - 工具层 `warnings` 逐条列出，标题为“工具层提示”。
5. **工况对比表**（两个及以上工况，沿用现有表格），表后加 `_temperature_trend(executions)` 生成的解读：
    - 按出口温度从低到高排序，用首尾两个工况计算 CH4 转化率变化（百分点）、H2 出口流量变化（kmol/h，取 `component_flows_kmol_h`）、CO/CO2 摩尔比变化、热负荷变化，写成一句。
    - 只有在组分同时含 Methane、CO、CO2 时，才加机理解释：转化率随温度上升时写“重整反应吸热，升温使平衡向产物方向移动”；CO/CO2 上升时写“水煤气变换放热，升温抑制变换”。实际方向与此相反时，不写机理，改写“与吸热重整的一般规律不一致，需要核查”。
6. 状态为 READY 且没有执行时，写“规格已编译并通过预检，尚未运行模拟（dry run）。”。**必须包含 `dry run` 字样**，已有测试依赖它。
7. **运行记录**：列出 `problems`。

**8.3 接入两处调用方**

- `nodes/explain.py`：只剩 `explain_node(state)`，返回 `{'explanation': render_report(view_from_state(state))}`。为兼容旧导入，从 `report` 重新导出原来的私有函数名。
- `pipeline.py`：删除 `_describe_ready` 和 `_describe_executed`，在 READY、UNSUPPORTED 和执行结束三处都改为 `run.explanation = render_report(view_from_run(run))`。`view_from_run` 里的执行项写成 `{**e.summary(), 'results': e.results()}`，与 execute 节点一致。
- `__main__.py` 单遍路径写 `explanation.txt` 时直接写 `run.explanation`，不再自己拼“假设”和“待澄清问题”，因为报告里已经有了。

**新增测试**（新建 `reactor_agent/test_report.py`，夹具用附录里的气化与重整 `result.json` 片段）：

- 同一个 view 分别经 `view_from_state` 和 `view_from_run` 生成，文本完全相同。
- 气化：主出口是 VAPOUR；LIQUID 标为固相碳；出现“氧平衡上限”，数值为 40.86%（用附录数字，`places=2`）；出现“主要受进料中的水量限制”。
- 重整两工况：出现“工况对比”；600°C 到 710°C 的 CH4 转化率变化是 +23.68 个百分点；出现“吸热”。
- 把两个工况的转化率对调后，不出现“吸热”，出现“需要核查”。
- Equilibrium 结果含 `RXN-1` 的 Q/K 行。
- adapter：`results()` 对缺少新字段的旧 `result.json` 不抛异常，新键为 None 或空。

## 步骤 9：`reactor_agent/extraction.py` 方程式识别

只把“箭头两侧都是化学式”的文字当成用户写出的方程式，并且只用方程式里的系数来核对模型给出的系数。现在的正则 `\d\s*[A-Z][a-z]?` 会命中 H2O 里的 `2O`、2.5MPa 里的 `5M`、80000Nm3 里的 `0N`，几乎任何请求都被当成“写了方程式”。于是重整里模型按元素守恒推出的 3H2 被误报为“系数无依据”，触发一个不该有的阻塞问题。

**9.1 新增 `written_equations(text) -> list[dict[str, float]]`**

1. 先把下标数字 `₀…₉` 换成普通数字（复用 `normalize` 里的同名转换表，或在本模块再定义一份）。
2. 箭头：`<=>`、`<->`、`⇌`、`⇄`、`↔`、`⟶`、`→`、`->`、`=>`、`=`。
3. 一项的形式：`(?:\d+(?:\.\d+)?\s*)?(?:[A-Z][a-z]?\d*)+`，即可选系数加化学式；多项之间用 `+` 连接。用一个正则匹配“若干项 + 箭头 + 若干项”。
4. 对每个匹配逐项校验：化学式拆成元素符号后，每个符号都必须在一个固定的元素集合里（至少包含 H、C、N、O、S、Cl、Ar、He、F、P、Na、K、Ca、Fe）。有任何一项不合格，就丢弃整条匹配。这样 `Nm3`（Nm 不是元素）和 `MPa`（M 不是元素）都会被排除。
5. 返回 `{化学式: 带符号系数}`，左侧为负，右侧为正；没写系数的按 1。

**9.2 改写现有函数**

- `states_numeric_equation(text)` 改为 `bool(written_equations(text))`。函数名保留，避免改动调用方；在文档字符串里说明它现在的含义是“写出了方程式”。
- `reaction_grounding_failures`：没有写出的方程式时返回 `[]`，与现在一致。有方程式时，“原文给出的系数”只取方程式各项系数的绝对值，不再取原文中出现的所有数字。模型给出的系数绝对值不大于 1 时照旧放过；大于 1 时必须与其中某个系数相等（容差沿用 `tolerance`），否则记为失败。
- `reaction_is_derived` 的逻辑不变（没有写出方程式就是推导所得），因为它调用的是 `states_numeric_equation`，会自动用上新规则。

删除 `_NUMERIC_EQUATION` 正则。

**新增测试**（`test_extraction.py`）：

| 输入 | 期望 |
| --- | --- |
| `甲苯 2C₇H₈ → C₆H₆ + C₈H₁₀` | `[{'C7H8': -2, 'C6H6': 1, 'C8H10': 1}]` |
| `主要反应：C+H2O → CO+H2` | 一条方程，四项系数绝对值都是 1 |
| `C + H2O = CO + H2` | 一条方程 |
| `进料是甲烷和水蒸气（摩尔比 1:2.7），压力 13.5 bar，H2O` | `[]` |
| `流量80000Nm3/h，压力2.5MPa` | `[]` |
| `Kp=2.3，T=380` | `[]` |

另外两条：重整原文（没有方程式）配模型给出的 `H2: 3`，`reaction_grounding_failures` 返回 `[]`，`reaction_is_derived` 为 True；甲苯原文配模型给出的 `苯: 3`，返回一条失败。

## 步骤 10：`reactor_agent/__main__.py` 交互式追问与 CLI

默认走状态图；暂停时在终端里逐题作答，回车采用默认答案；`--accept-defaults` 让录屏全自动跑完；`--answer` 不带 `--out` 时自动续上最近一次暂停的运行。

**10.1 参数调整**

| 参数 | 变化 |
| --- | --- |
| `--graph` | 保留但不再需要，帮助文字写“默认行为，保留以兼容旧脚本” |
| `--single-pass` | 新增，走旧的 `run_pipeline` 单遍路径 |
| `--accept-defaults` | 新增，每个有默认答案的问题自动采用默认值 |
| `--no-input` | 新增，不在终端提问，打印问题和恢复命令后退出（退出码 3） |
| `--answer ID=VALUE` | 不带 `--out` 时，自动定位最近一次暂停的运行目录 |

**10.2 内置场景表 `SCENARIOS`**

- 甲苯的 `phase` 从 `'liquid'` 改为 `'unknown'`。相态只影响有动力学时的 CSTR/PFR 选择，甲苯没有动力学，写死 liquid 没有依据；spec 里的反应相态已由步骤 5 固定为 combined。
- 三道题的 `text` 保持题目原文，不要改。

**10.3 拆出可测试的作答循环**

新增函数：

```python
def drive_graph(graph, first_input, config, answer_fn) -> dict
```

它先 `invoke(first_input, config)`；只要结果里有 `__interrupt__`，就取出 `payload['questions']` 交给 `answer_fn(questions)`。`answer_fn` 返回 None 表示放弃，函数原样返回当前状态；返回字典时调用 `graph.invoke(Command(resume=answers), config)` 继续。图自身的 `MAX_CLARIFICATION_ROUNDS` 已经限制了轮数，这里不用再计数。

提供三个 `answer_fn`：

- `defaults_answerer(questions)`：每题取 `default`；有任何一题没有默认值，就打印该题并返回 None。
- `terminal_answerer(questions)`：逐题打印问题、原因和“[回车 = 默认：…]”，用 `input('> ')` 读取。空输入且有默认值时，采用默认值并打印“已采用默认：…”；空输入且无默认值时重新提示，最多三次，仍为空就返回 None。
- `no_input_answerer(questions)`：直接返回 None。

`main()` 的选择顺序：指定 `--accept-defaults` 时用第一个；否则在 `sys.stdin.isatty()` 为真且未指定 `--no-input` 时用第二个；其余情况用第三个。

**10.4 暂停标记与 `--answer` 自动定位**

- 运行停在暂停状态时，在运行目录写 `paused.json`，内容为 `{'label', 'thread_id', 'questions', 'paused_at'}`，并打印恢复命令：`python -m reactor_agent --scenario <名> --answer <id>=<值>`。
- 运行结束（不再暂停）时删除 `paused.json`。
- 新增 `latest_paused_run(label, base) -> Path | None`：在 `agent-runs/` 下找 `run_folder` 命名规则匹配该标签、且含 `paused.json` 的目录，取修改时间最新的一个。
- 给了 `--answer` 而没给 `--out` 时用它；找不到就打印“没有找到等待回答的运行，请用 --out 指定目录”，退出码 2。

**10.5 结束时的输出**

运行结束后，先调用现有的 `_print_graph_state`，再在控制台完整打印 `state['explanation']`（已经执行过 `_use_utf8_console`，打印失败时由 `_print_ascii` 兜底），最后 `dump_state` 写出产物。单遍路径同理打印 `run.explanation`。

**新增测试**（新建 `reactor_agent/test_cli.py`，不调用真实模型）：

- 用 `test_graph.py` 里同样的假客户端构建气化图，`drive_graph(..., defaults_answerer)` 一次跑完，`status == 'READY'`。
- `no_input_answerer` 下停在暂停状态，`__interrupt__` 存在。
- `terminal_answerer` 配合 `unittest.mock.patch('builtins.input', side_effect=['', ''])`：两题都采用默认值，跑完。
- `latest_paused_run`：在临时目录建两个带 `paused.json` 的目录和一个不带的，返回较新的那个带标记的目录。
- `SCENARIOS['toluene']['phase'] != 'liquid'`。

## 步骤 11：旧测试更新与新增测试汇总

只有下表中的旧测试允许修改，因为它们写的是已被工具层验收推翻的旧结论。改完之后，旧测试应该一项不少地继续存在（改名可以，删除不行），其他所有旧测试不改一字也要通过。

**允许修改的旧测试**

| 文件 | 测试 | 旧预期 | 新预期 |
| --- | --- | --- | --- |
| `test_selection.py` | `test_unsupported_combinations_are_not_silently_allowed` | 等温 equilibrium 为 unsupported | 删去这一行；加入“绝热 equilibrium”和“含固体的等温 equilibrium”两行，都为 unsupported |
| `test_selection.py` | `test_solid_carbon_gibbs_is_experimental_not_verified` | experimental | 改名为 `test_solid_carbon_gibbs_is_verified_via_saturation`，预期 verified，规则 `gibbs_isothermal_solid_saturation` |
| `test_normalize.py` | `SolidPhaseIsCarried.test_the_capability_becomes_experimental` | experimental | 改名为 `test_the_capability_is_the_saturation_route`，预期 verified |
| `test_normalize.py` | `DelegatedChoices.test_a_choice_is_made_and_recorded_as_an_assumption` | `scope` 含 `may be chosen` | `scope` 含 `可以自定` |
| `test_compiler.py` | `ReformerCompilation.test_gibbs_spec_carries_no_reactions` | `reactions == []` | 按步骤 5 改为 Equilibrium 版本 |
| `test_compiler.py` | 模块文档字符串 | 引用 `acceptance-20261002-144131-d442afae` | 改为引用验收 `20261003-105341-4467d9c9` |

**修改流程**：先改代码，让对应旧测试失败；确认失败原因正是表中写的结论变化，再改测试。失败原因不同的，说明代码有问题，回去修代码。

**新增测试汇总**（细节见各步骤）

| 文件 | 内容 | 来源步骤 |
| --- | --- | --- |
| `test_compiler.py` | Nm³ 契约校验、Question 默认值 | 1 |
| `test_selection.py` | 能力表各行、哈希不一致降级、Equilibrium 闭合网络、关键词误判、候选产物补齐、甲苯 verified | 2、3 |
| `test_normalize.py` | Nm³ 默认题与确认路径、流量锚定、二甲苯假设、id 跨进程稳定、Gibbs 计划补齐 | 4、6 |
| `test_compiler.py` | 反应块按类型、ASCII 名、饱和碳、去重、煤默认题 | 5 |
| `test_graph.py` | 编译失败为 FAILED、问题带默认值、Nm³ 回答解析、气化一轮作答跑通 | 6、7 |
| `test_report.py`（新） | 两条路径报告一致、固相碳、氧平衡上限、温度对比与反例、Q/K 行、adapter 兼容旧结果 | 8 |
| `test_extraction.py` | 方程式识别正反例、系数核对 | 9 |
| `test_cli.py`（新） | 作答循环三种模式、暂停目录定位、甲苯相态 | 10 |

**能力表降级测试的写法**：

```python
from unittest import mock
from reactor_agent import capabilities as caps

def test_a_changed_tool_layer_downgrades_verified(self):
    caps.acceptance_state.cache_clear()
    try:
        with mock.patch.object(caps, '_file_sha256', return_value='0' * 64):
            caps.acceptance_state.cache_clear()
            status = caps.combination_status('equilibrium', 'isothermal')
        self.assertEqual(status['status'], 'experimental')
        self.assertIn('不一致', status['reason'])
    finally:
        caps.acceptance_state.cache_clear()
```

`finally` 里必须清缓存，否则后续测试会拿到降级结果。

**`scripts/run-all-tests.cmd`**：加入 `reactor_agent.test_report` 和 `reactor_agent.test_cli` 两组，编号相应改为 `[n/13]`。

## 步骤 12：README、cmd 脚本与文档

文档要和新行为一致，尤其要删掉“Equilibrium 不可用”和“气化必须拒绝”这两类旧结论。不能写“智能体端到端已通过真机验收”，那要等你在工作站跑完才算。

**12.1 先找出所有过时说法**（`docs/` 下的文档已另行改好，见 12.4，这里只查其余文件）：

```
grep -rn "拒绝\|refuse\|BLOCKED\|FixedK\|Ln(K)\|unsupported\|experimental\|--graph" README.md REPORT.md PROJECT_PLAN.md Run-Agent-On-Workstation.cmd
```

逐条判断：描述工具层历史的保留，但要标明是历史；描述当前行为的按新行为改写。

**12.2 `README.md`**

- 顶部提示框：改为“工具层已通过验收 `20261003-105341-4467d9c9`（ALL_SCENARIOS_PASS）；智能体已同步，离线测试通过，端到端真机运行待执行”。
- “当前状态”的工具层表格：甲苯 Conversion；重整 Equilibrium 两个工况，另有 Gibbs 对照；气化走饱和碳路线。旧的 `d442afae` 表格移到“历史验收”小节。
- Agent 层表格：甲苯 READY；重整 READY（Equilibrium，两个工况，自定流量甲烷 1000 kmol/h 约 128 kt/a）；气化先 WAITING_INPUT（两题都带默认答案），回车后 READY。
- “能力范围与边界”整节按步骤 2 的判定表重写。删掉“Ln(K) 源恒为 FixedK=2”一行，删掉“气化场景的输入保护”一段，改为一段“气化需要确认的两项”：Nm³ 的物流与标准状态、煤按纯碳；说明默认答案是什么，以及为什么必须问。
- “用自然语言驱动”一节：默认即状态图；示例改为 `python -m reactor_agent --scenario gasification`（终端逐题作答），`--accept-defaults`，`--answer` 不带 `--out`，`--single-pass`。
- 目录结构里加上 `report.py`、`test_report.py`、`test_cli.py`；测试数量按实际运行结果填写。

**12.3 `Run-Agent-On-Workstation.cmd`**，改为四步，全部使用新的运行目录：

| 步骤 | 命令 | 预期 |
| --- | --- | --- |
| 1/4 | `--scenario toluene --execute --accept-defaults --out "%RUN%\toluene"` | 退出码 0，转化率 50% |
| 2/4 | `--scenario smr --execute --accept-defaults --out "%RUN%\smr"` | 退出码 0，两个工况都是 Equilibrium 且 PASS |
| 3/4 | `--scenario gasification --execute --accept-defaults --out "%RUN%\gasification"` | 退出码 0，饱和碳路线 PASS |
| 4/4 | `--scenario gasification --out "%RUN%\gasification-interactive"` | dry run，终端弹出两个问题，演示时按两次回车 |

文件末尾的“预期结果”改为：

- 甲苯：转化率 49.99999999999999%，出口 Toluene 54.2648、Benzene 27.1324、三种二甲苯各 9.0441 kmol/h（与历史基线逐位相同）。
- 重整：Equilibrium 的 CH4 转化率与 Gibbs 历史值（710°C 54.035809%，600°C 30.352385%）相差不超过 0.5 个百分点，热负荷相对差不超过 1%，Q/K 判定 PASS。有 `baseline_candidate.json` 时以它为准。
- 气化：CO 收率约 40.14%，碳转化率约 41.16%，外供热约 84656 kW（来自饱和碳单独运行 `native-flow-20261003-091907`，以验收 `20261003-105341` 的结果为准）。

删掉“gasification: refused, 0 case files created”和“must REFUSE”相关的所有行与注释。

**12.4 `docs/` 下的文档（已改好，随本计划提供）**

1. 用提供的版本直接覆盖：`docs/REVIEW_BRIEF.md`、`docs/TOOL_REFERENCE.md`、`docs/EQUILIBRIUM_INTEGRATION.md`、`docs/REMOTE_VALIDATION.md`、`docs/AGENT_IMPLEMENTATION_PLAN.md`，并新增 `docs/review-20261003/README.md`。`TOOL_REFERENCE.md` 是 CRLF 换行，覆盖时不要转换。
2. 删除 `docs/NATIVE_FLOW_UPDATE.md`。它描述的 `MolarFlow.SetValue(..., 'Nm3/h')` 路径已被验收方案取代，没有任何文件引用它。
3. `docs/GWOA_MIGRATION.md` 不动。
4. 这些文档描述的是**本计划完成后**的行为。实现结束后逐条核对 `docs/REVIEW_BRIEF.md` 的第 2 节（陷阱 1、2、11、12）和第 3 节（命令、预期、退出码），确认与实际一致：问题的默认答案文字、CLI 输出、案例名后缀 `-710C`/`-600C`、反应名 `RXN-1`、13 个测试套件。有出入时，优先改代码使其符合计划；确需偏离计划时改文档，并在 `docs/AGENT_FIX_NOTES.md` 记录。
5. 新建 `docs/AGENT_FIX_NOTES.md`：每一步的实际改动、偏离本计划的地方及理由、最终测试数量。`REVIEW_BRIEF.md` 和 `EQUILIBRIUM_INTEGRATION.md` 都引用了它。
6. `PROJECT_PLAN.md`：在进度部分加一行本次修复和待办的工作站运行。

**12.4b 根目录的 `RELEASE_MANIFEST.json`**：它是 10-02 旧发布包的哈希清单，与当前文件不符，却会被打进提交包。删除它，并从 `scripts/build_submission.py` 的 `INCLUDE_FILES` 里去掉这一项（`build_release.py` 会在发布 ZIP 内部重新生成它，不受影响）。

**12.5 打包**：`python scripts\prepare_git.py` 通过后，运行 `python scripts\build_submission.py`，确认生成的 ZIP 里有 `reactor_agent/report.py` 和 `docs/tool-acceptance-20261003-105341.json`。

## 步骤 13：中文网页界面与运行时填写凭据（PROJECT_PLAN 的 P1-F）

在本机浏览器里完成“填模型连接 → 输入需求 → 回答追问 → 看结果”，不需要命令行。凭据只在运行时填写，**不写进代码，也不进入任何会提交或打包的文件**。这一条是 `TECH_STACK.md` 里已经记录的项目要求，原因是 AI 对话记录和仓库都要交付，key 一旦进入提交历史，之后删掉也会留在历史里。

**13.1 技术约束**

- 只用标准库 `http.server.ThreadingHTTPServer`，页面是一个内联 CSS/JS 的 HTML 文件，不引入新依赖（执行规则 7）。
- 只监听 `127.0.0.1`，默认端口 8765，可用 `--port` 修改。启动命令 `python -m reactor_agent.web`，另加双击入口 `Run-Agent-UI.cmd`。
- 新文件：`reactor_agent/web.py`（服务端）、`reactor_agent/web_static/index.html`（页面）、`reactor_agent/test_web.py`（测试）。

**13.2 凭据规则（必须全部满足）**

1. 页面“模型连接”区有三个输入框：地址、模型名、Key。Key 用 `type="password"`。
2. 启动时读取环境变量 `TR_BASE`、`TR_MODEL`、`TR_KEY`。地址和模型名预填到页面；Key 不回传，只显示“已设置”或“未设置”。
3. 测试时如果不想每次输入，可以在**项目根目录**放一个 `.env` 文件，写 `TR_KEY=...` 等行。程序只读不写，用标准库逐行解析 `KEY=VALUE`。`.env` 已在 `.gitignore` 里；它不在 `build_submission.py` 的打包清单里，打包时不会带上。不要把 `.env` 放进 `reactor_agent/` 等会被整目录打包的文件夹。
4. 优先级：页面填写 > 环境变量 > `.env`。
5. 页面填入的 Key 只保存在服务进程内存里，进程退出即丢失。不写入检查点、`state.json`、`explanation.txt`、日志、错误信息，也不写入浏览器 `localStorage`。
6. `GET /api/settings` 只返回 `{base, model, key_set}`，任何接口的响应都不包含 key。
7. 代码和测试里不得出现真实 key；测试一律用假 transport，不需要 key。提交前 `python scripts\prepare_git.py` 的凭据扫描必须通过。

**13.3 页面功能**

1. **需求输入**：场景下拉框（甲苯、重整、气化、自定义）。选中内置场景时，把题目原文填进文本框，可以再编辑。
2. **运行方式**：默认 dry run。选“真实执行 HYSYS”时，必须勾选“工作站已打开 HYSYS 且没有其他模拟在运行”。
3. **追问区**：暂停时逐题显示问题、原因，默认答案预填在输入框里，用户可改可直接提交。提交后继续运行，同一问题不再出现。
4. **结果区**：
    - 选型结论和理由，理论选型与实际执行不同时写明原因。
    - 假设分三组显示：本系统选定、用户确认、推导所得。
    - 工况对比表与温度趋势解读。
    - 气化的 CO 收率放在醒目位置，旁边是碳转化率和氧平衡上限。
    - 校验结果和工具层提示。
    - 报告全文。
    - 下载 `spec-*.json`、`explanation.txt`、`state.json` 的链接。
5. **状态栏**：显示 `WAITING_INPUT`、`READY`、`RUNNING`、`PASS`、`PARTIAL`、`FAILED`、`UNSUPPORTED`。

**13.4 服务端接口**

| 接口 | 作用 |
| --- | --- |
| `POST /api/settings` | 接收 `{base, model, key}`，存进内存，返回 `{key_set}` |
| `GET /api/settings` | 返回 `{base, model, key_set}` |
| `POST /api/run` | 接收 `{scenario, text, execute}`，新建运行目录和 thread_id，用与 CLI 相同的 `build_graph` 开始运行，返回 `{run_id, status, questions 或 report}` |
| `POST /api/answer` | 接收 `{run_id, answers}`，以 `Command(resume=answers)` 继续 |
| `GET /api/run/<run_id>` | 只读当前状态。刷新页面不会重新计算，也不会再次调用模型 |
| `GET /api/run/<run_id>/files/<name>` | 只允许下载该运行目录内白名单里的文件，拒绝 `..`、绝对路径和其他文件 |

- 运行目录与 CLI 相同，都在 `agent-runs/` 下；场景表复用 `__main__.SCENARIOS`，作答循环复用步骤 10 的逻辑。
- 同一时间只允许一个真实执行，另一个执行请求返回“工作站忙”；dry run 不受限制。
- 模型调用失败时，在页面显示明确的错误，并给出已经算出的数值（如果有），不猜结果。

**13.5 测试（`reactor_agent/test_web.py`，不联网、不连 HYSYS）**

- `POST /api/settings` 设置一个假 key 后，`GET /api/settings`、任意运行的响应、运行目录里的所有文件、检查点文件中都找不到这个字符串。
- 用假客户端运行气化：第一次返回两个问题且都带默认答案；用默认答案作答后状态为 `READY`。
- 连续两次 `GET /api/run/<run_id>`，假 transport 的调用次数不增加。
- 下载接口拒绝 `../`、绝对路径和白名单以外的文件名。
- 服务只绑定 `127.0.0.1`。
- `.env` 解析：忽略空行和 `#` 注释，值两侧的引号被去掉。

**13.6 文档**

- `README.md` 新增“网页界面”一节：启动方式、凭据填写方式、`.env` 的用法和注意事项。
- `docs/REVIEW_BRIEF.md` 第 5 节“没有图形界面”一行改为“有本机网页界面（`python -m reactor_agent.web`），只监听 127.0.0.1”；第 4 节第 8 条“凭据是否真的不会落盘”补上相关测试 `test_web.py`。
- `Run-Agent-On-Workstation.cmd` 保持命令行流程不变，作为主要的验收证据；网页界面用于录屏演示。
- `scripts/run-all-tests.cmd` 加入 `reactor_agent.test_web`，编号改为 `[n/14]`；`docs/REVIEW_BRIEF.md` 第 3 节的“13 个套件”相应改为 14。

## 离线验收标准

下面每一项都满足，才算本地这一轮完成；任何一项不满足都不要打包交付。

- [ ] `hysys_tools/` 与 `docs/tool-acceptance-20261003-105341.json` 的哈希仍然全部一致（重跑步骤 0 的脚本）。
- [ ] `git diff baseline -- hysys_tools` 输出为空。
- [ ] `python -m unittest discover -s reactor_agent -t .` 全部通过，数量多于 242。
- [ ] `python -m unittest discover -s hysys_tools -t .` 仍为 184 项通过，`python -m hysys_tools.selfcheck` 退出码 0。
- [ ] `scripts\run-all-tests.cmd` 显示 ALL SUITES PASSED。
- [ ] 步骤 11 表中的旧测试都还在（改名可以），其余旧测试一字未改。
- [ ] 三个内置场景的 dry run 结果如下表（需要 `TR_KEY`，在本机即可跑，不需要 HYSYS）。

| 命令 | 退出码 | 必须看到 |
| --- | --- | --- |
| `python -m reactor_agent --scenario toluene --no-input` | 0 | `conversion (verified`，spec 里反应名 `RXN-1`、相态 `combined` |
| `python -m reactor_agent --scenario smr --no-input` | 0 | `equilibrium (verified`，两个 spec，案例名以 `-710C`、`-600C` 结尾，进料甲烷 1000 kmol/h |
| `python -m reactor_agent --scenario gasification --no-input` | 3 | 两个问题，各带默认答案；没有重复的 Nm³ 问题 |
| `python -m reactor_agent --scenario gasification --accept-defaults` | 0 | `gibbs (verified`，spec 含 `flow_input: normal_volume` 和 `solid_carbon: saturation`，组分含 CO2、Methane |
| 上一条暂停后执行 `--scenario gasification --answer q-volumetric-flow=默认 --answer q-coal-definition=按纯碳处理`（不带 `--out`） | 0 | 自动找到最近的暂停目录并继续 |

- [ ] 用 `--accept-defaults` 生成的气化 spec 与 `hysys-tool-layer.zip` 里的 `specs/coal-slurry-gasification.json` 对比，下列字段相同：组分集合、进料组成与基准、`total_flow`、`flow_input`、两个标准状态、反应器 `kind`、`thermal_mode`、`outlet_temperature`、`solid_carbon`。
- [ ] 重整 spec 与 `specs/methane-steam-reforming-710C.json` 对比：反应器 `kind`、两个反应的计量系数与 `phase`、出口温度相同；进料按摩尔分数加总流量表示，换算后甲烷 1000、水 2700 kmol/h。
- [ ] `docs/AGENT_FIX_NOTES.md` 已写好，记录了偏离计划的地方。
- [ ] 网页界面：`python -m reactor_agent.web` 能在 `http://127.0.0.1:8765` 打开；用页面填 key 跑一次气化 dry run，两题采用默认答案后得到 READY。
- [ ] 凭据：在仓库里搜索你实际使用的 key 前若干位，除被忽略的 `.env` 外没有任何命中；`python scripts\prepare_git.py` 通过；提交包里没有 `.env`。

## 工作站端到端验收（由你执行）

这一步本地 Agent 做不了，需要装有 HYSYS 的工作站。跑完并录屏，才能说“智能体端到端通过”。

**准备**

1. 把步骤 12.5 生成的提交包解压到工作站上的**新目录**，不要覆盖已验收的工具层目录。
2. 使用已验收的 Python 环境，确认 `pydantic`、`langgraph`、`pywin32` 都能导入。
3. 启动 HYSYS，关掉弹窗和遗留案例；不要同时运行其他模拟脚本。
4. 在同一个命令行窗口里执行 `set TR_KEY=<你的 key>`。
5. 开始录屏。

**运行**

1. 双击 `Run-Agent-On-Workstation.cmd`。前三步全自动执行，第四步会在终端弹出两个问题，各按一次回车采用默认值。
2. 想展示“自然语言原样输入”，再分别运行三次 `python -m reactor_agent --text "<题目原文>" --execute`，原文从 `__main__.py` 的 `SCENARIOS` 里复制。遇到问题时按回车采用默认值。
3. 注意：用 `--text` 时不会自动放行“自定流量”。如果模型在重整题里编了一个流量，会出现 `q-ungrounded:feed_total` 问题，这类问题没有默认值。回答 `3700 kmol/h` 即可，对应甲烷 1000 kmol/h。

**判定通过**

| 工况 | 必须满足 |
| --- | --- |
| 甲苯 | `PASS`；转化率 50%；出口与历史基线逐位相同；报告显示 verified |
| 重整 710°C / 600°C | 两个工况都 `PASS`；反应器为 Equilibrium；报告里有每个反应的 Q/K 行且判定 PASS；CH4 转化率与 Gibbs 历史值相差不超过 0.5 个百分点；有两温度对比与解读 |
| 气化 | `PASS`；`solid_carbon_saturation` 存在；报告标出 LIQUID 为固相碳；有 CO 收率、碳转化率、氧平衡上限和外供热口径 |
| 交互演示 | 终端弹出 Nm³ 与纯碳两个问题，回车后不再重复提问 |

**带回来的东西**

- 整个 `agent-runs\workstation-*\` 目录：每个工况的 `spec-*.json`、`state.json`、`explanation.txt`，以及各次尝试目录里的 `result.json`、`steps.json` 和 `.hsc`。
- 录屏文件。
- 任何失败工况的 `worker-stderr.txt`。

失败时不要在工作站上改代码，把以上材料带回来再定位。

## 附录：参考 spec、测试夹具与关键数字

**A. 对照用的已验收 spec**（都在 `hysys-tool-layer.zip` 的 `specs/` 里）

| 文件 | 用途 |
| --- | --- |
| `toluene-disproportionation.json` | 甲苯：反应相态 `combined`，三种二甲苯各 1/3，绝热 |
| `methane-steam-reforming-710C.json`、`-600C.json` | 重整：Equilibrium，两个 `vapour` 反应，进料甲烷 1000、水 2700 kmol/h |
| `coal-slurry-gasification.json` | 气化：`flow_input: normal_volume`，0°C / 101.325 kPa，`solid_carbon: saturation`，六个组分 |

**B. 气化结果夹具**（从 `tool-layer-runs/native-flow-20261003-091907-5550b747/gasification/result.json` 裁剪，数值保留 4 到 6 位；`test_report.py` 直接用）

```json
{
  "status": "PASS", "reactor_kind": "gibbs",
  "case_file": "agent-gasification-saturation.hsc",
  "feed_molar_flows_kmol_h": {"Carbon": 2533.8002, "H2O": 1035.4024},
  "outlet": {
    "VAPOUR": {"molar_flow_kmol_h": 2033.879, "temperature_C": 1400.0, "pressure_kPa": 4000.0,
               "mole_fractions": {"Carbon": 0.0, "H2O": 0.005527, "CO": 0.500119,
                                  "Hydrogen": 0.481726, "CO2": 0.001716, "Methane": 0.010912}},
    "LIQUID": {"molar_flow_kmol_h": 1490.9346, "temperature_C": 1400.0, "pressure_kPa": 4000.0,
               "mole_fractions": {"Carbon": 1.0, "H2O": 0.0, "CO": 0.0,
                                  "Hydrogen": 0.0, "CO2": 0.0, "Methane": 0.0}}},
  "component_flows_kmol_h": {"Carbon": 1490.9346, "H2O": 11.2413, "CO": 1017.1811,
                             "Hydrogen": 979.7721, "CO2": 3.49, "Methane": 22.1945},
  "heat_duty_kW": 84656.26,
  "checks": {
    "worst_element_relative_error": 1.1e-15, "mass_relative_error": 5.9e-16,
    "reactant_conversion_percent": {"Carbon": 41.1582, "H2O": 98.9143},
    "co_yield": {"definition": "(n_CO_out - n_CO_in) / n_C_feed * 100%",
                 "co_yield_percent": 40.1445, "carbon_conversion_percent": 41.1582,
                 "dry_outlet_kmol_h": 2022.6377, "co_mole_fraction_dry": 0.502898},
    "heat_duty_scope": "External heat needed to hold the stated outlet temperature. ...",
    "condensed_phase_location": {"verdict": "CONDENSED_PHASE_ONLY"},
    "gibbs_equilibrium": {"verdict": "CONSISTENT", "orders_from_equilibrium": 0.00482},
    "independent_duty": {"verdict": "CONSISTENT", "relative_deviation": -0.00652}},
  "solid_carbon_saturation": {
    "carbon_conversion_x": 0.411582, "water_limited_x_max": 0.612341,
    "carbon_activity": {"via_methanation": 1.000003, "via_water_gas": 0.993607,
                        "via_boudouard": 0.994444, "spread_decades": 0.002787},
    "duty_by_reactor_kW": {"conversion": 64348.06, "gibbs": 20308.2},
    "library_carbon_gibbs_used": false},
  "warnings": [], "assumptions": [], "open_questions": []
}
```

**C. 重整结果夹具**（历史 Gibbs 验收 `acceptance-20261002-144131-d442afae`，用于对比表与温度趋势测试；`heat_duty_scope` 用 B 里同样的等温文字）

| 项目 | 600°C | 710°C |
| --- | --- | --- |
| 状态 | PASS | PASS |
| 热负荷 kW | 20159.498 | 39988.6148 |
| CH4 转化率 % | 30.352385 | 54.035809 |
| H2O 转化率 % | 20.615656 | 31.929486 |
| 出口 VAPOUR kmol/h | 4307.0477 | 4780.7162 |
| Methane kmol/h | 696.4761 | 459.6419 |
| H2O kmol/h | 2143.3773 | 1837.9039 |
| CO kmol/h | 50.425 | 218.6201 |
| Hydrogen kmol/h | 1163.6704 | 1942.8123 |
| CO2 kmol/h | 253.0989 | 321.738 |

摩尔分数可由组分流量除以出口总流量得到；LIQUID 流量为 0。

**D. Equilibrium 的 Q/K 夹具**。手边没有 Equilibrium 真机结果，测试里用下面这个人工构造的片段，形状与工具层 `equilibrium.check_outlet` 的返回一致：

```json
{"verdict": "PASS", "ln_ratio_tolerance": 0.05,
 "reactions": [{"reaction": "RXN-1", "Q_over_K": 1.0003, "ln_Q_over_K": 0.0003, "verdict": "PASS"},
               {"reaction": "RXN-2", "Q_over_K": 0.9998, "ln_Q_over_K": -0.0002, "verdict": "PASS"}]}
```

工作站跑出真实结果后，可以把它换成真实片段。

**E. 报告测试应得到的派生数字**

| 量 | 计算 | 结果 |
| --- | --- | --- |
| 气化氧平衡上限 | 1035.4024 / 2533.8002 | 40.86% |
| CO 收率与上限之比 | 40.1445 / 40.8636 | 0.982 |
| CH4 转化率变化 | 54.035809 − 30.352385 | +23.68 个百分点 |
| CO/CO2 摩尔比 | 50.425 / 253.0989 → 218.6201 / 321.738 | 0.199 → 0.679 |
| H2 出口流量 | 600°C → 710°C | 1163.67 → 1942.81 kmol/h |
| Nm³ 换算 | 80000 / 22.41397 | 3569.20 kmol/h |
| 重整自定流量 | 1000 × 16.043 × 8000 / 1e6 | 约 128.3 kt/a 甲烷 |

**F. 修复前的问题证据**（`verification/legacy-model-evaluation/smoke-agent.json`）：气化的阻塞问题列表里，同一句 Nm³ 问题出现了两次，分别来自 normalize 和 compiler；甲苯 spec 的反应名是“歧化反应”，相态是 `liquid`；重整 spec 进料总流量 1370.37 kmol/h，即甲烷只有 370 kmol/h，反应器是 Gibbs。修完后用同一场景重跑，这四处都应改变。
