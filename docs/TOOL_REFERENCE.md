# hysys_tools 工具参考

本文档面向工具调用方。当前版本 `2026-10-03-equilibrium-integration-1` 已接入气相等温 Equilibrium 和饱和碳气化主验收，正式版远程验收待运行；下文“已验证”指历史路径。新增接口、Q/K 门控与操作步骤以 [本次接入说明](EQUILIBRIUM_INTEGRATION.md) 为准。

## 1. 它做什么

一次调用只建一个完整的 HYSYS 案例，流程固定：

读入一份 spec JSON → 新建空白案例 → 配置物性包和组分 →（转化反应器）配置反应和反应集 → 建进料、产品物流、能量物流和反应器 → 求解 → 独立校验 → 保存 `.hsc` → 关闭案例 → 写出 `result.json`

调用方不接触 COM，只做两件事：写 spec，读 result。

## 2. 运行前提和硬约束

- **环境**：Windows 工作站，HYSYS V15 已经打开，并且没有弹窗挡着。Python 3.12 + pywin32（远程已验证）。没有 HYSYS 的机器只能跑离线自检 `python -m hysys_tools.selfcheck`。
- **一次调用 = 一个案例。** HYSYS 的 COM 对象不能跨进程，所以没有"打开一个案例、之后再接着改"的用法。要算多个工况，就调用多次。
- **每次调用的 `--folder` 必须是一个新目录。** 案例文件路径一旦重复，HYSYS 会把它解析到一个还开着的旧案例上，返回的"新案例"就不是空白的，会被空白检查拒绝。工具本身不会替你生成唯一目录，这件事由调用方负责，建议目录名里带时间戳。
- **只能串行调用。** 同一个 HYSYS 实例上并发跑多个调用，没有验证过。
- **控制台只输出 ASCII**（远程控制台是 cp1252）。完整信息，包括中文，都在 UTF-8 编码的 `result.json` 里。程序化调用时应该读 `result.json`，不要去解析控制台输出。
- **不碰别人的案例。** 工具只会保存和关闭它自己新建、并且确认是空白的案例。如果 `SimulationCases.Add` 返回的是一个已有内容的案例，工具不会动它，只记一条 warning。

## 3. 快速上手

```bat
cd hysys-agent
python -m hysys_tools --health-check
python -m hysys_tools --write-examples specs
python -m hysys_tools --spec specs\toluene-disproportionation.json --folder runs\20261002-120000-toluene
```

跑完后，`runs\20261002-120000-toluene\` 里会有 `result.json`、`steps.json` 和 `agent-toluene.hsc`。

`Run-Tool-Layer.cmd` 会按顺序跑完四个示例（健康检查 → 写示例 → 甲苯 → 重整 710/600°C → 气化），输出到 `tool-layer-runs\<时间戳>\`。

## 4. 命令行

| 参数 | 作用 |
|---|---|
| `--health-check` | 检查 COM 连接，在 stdout 打印 JSON：`status`、`application`、`python`、`case_count`。PASS 时退出码 0，否则 1 |
| `--validate-only` | **不碰 HYSYS**，只检查 spec，毫秒级返回。打印 `ok`、`errors`、`warnings`、`readback`；退出码 0 表示没问题，1 表示有问题，2 表示 spec 读不进来。**会把能发现的问题一次全部列出**，所以一轮就能修完。建议 agent 每次先跑这个再跑 `--spec` |
| `--list-capabilities` | 打印能力说明 JSON（反应器类型及各自验证状态、进料基准、流量/温度/压力单位、支持的组分、注意事项），退出码 0 |
| `--write-examples DIR` | 把四份示例 spec 写到 DIR，退出码 0 |
| `--spec FILE` | 要执行的 spec 文件。`build_case` 内部会先跑一次预检，所以 spec 有问题时**不会创建任何案例** |
| `--folder DIR` | 案例文件和结果写到这里。默认是 spec 所在目录，**不要用默认值**（原因见第 2 节） |
| `--result FILE` | 结果文件路径，默认 `<folder>\result.json`。父目录会被自动创建，所以连接失败时结果也不会丢 |

`--spec` 的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | `status` = `PASS` |
| 1 | `status` = `FAILED` 或 `CANNOT_CONNECT_TO_HYSYS` |
| 2 | `SPEC_ERROR`：spec 文件读不进来（不是合法 JSON、不是对象，或 `schema` 前缀不对）。也会写一份 `result.json`（`status` = `SPEC_ERROR`） |

`--validate-only` 的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | `ok` = true |
| 1 | `ok` = false，看 `errors` |
| 2 | spec 文件本身读不进来 |

`<folder>` 里的输出文件：

| 文件 | 什么时候有 |
|---|---|
| `result.json` | 正常退出路径均尝试写出；进程被终止或磁盘异常时可能缺失，调用方须检查 |
| `steps.json` | 进入 build_case 后写出，内容和 result 的 steps 对应；CLI 预检或连接失败时没有 |
| `<case_name>.hsc` | PASS |
| `<case_name>-FAILED.hsc` | FAILED，**并且案例确实是本次新建的**（见第 10 节第 2 条）。可以用 HYSYS 界面打开，检查是哪里出的问题 |

## 5. Spec 格式

### 5.1 顶层字段

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `schema` | 否 | `hysys-agent/spec/1` | 前缀必须是 `hysys-agent` |
| `case_name` | 否 | `agent-case` | 案例文件名，不带扩展名。注意：真机上 HYSYS 读回的 `case.Name` 永远是 `"Case"`，这个字段只决定文件名 |
| `scenario` | 否 | — | 工具不使用，仅作记录 |
| `fluid_package` | 是 | — | 见 5.2 |
| `feeds` | 是 | — | 列表，**只支持一项，多项会被预检拒绝**，见 5.3 |
| `reactions` | 视反应器而定 | `[]` | 转化反应器必填；Gibbs 反应器不使用（只检查元素是否平衡），见 5.4 |
| `reactor` | 是 | — | 见 5.5 |
| `assumptions` | 否 | `[]` | 字符串列表，原样复制到结果里 |
| `open_questions` | 否 | `[]` | 字符串列表，原样复制到结果里 |

### 5.2 `fluid_package`

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `name` | 否 | `AGENT-PR` | 物性包名称 |
| `property_package` | 否 | `PengRob` | 只有 `PengRob` 是验证过的。接受 `PengRob`、`Peng-Robinson`、`PengRobinson`、`PR`、`PR78` 等常见写法，**预检和执行都会统一映射成 `PengRob`**；其他值会被拒绝，不会猜。注意 `Peng-Robinson` 是 HYSYS 读回时的显示名，但作为输入也被接受 |
| `components` | 是 | — | 组分清单。接受第 6 节的别名（如 `CH4`、`steam`），**预检和执行都会用同一个映射转成库名**（如 `Methane`、`Water`）再交给 HYSYS。必须包含所有进料组分，以及所有可能出现的产物：Gibbs 反应器只会在这个列表里分配产物 |

### 5.3 `feeds[0]`

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `name` | 否 | — | **不使用**，进料物流固定叫 `FEED` |
| `basis` | 否 | `molar_fraction` | `molar_fraction`（同 `mole_fraction`）、`mass_fraction`（同 `weight_fraction`）、`molar_flow`（同 `mol_flow`）、`mass_flow` |
| `fractions` | 分数基准时必填 | — | `{组分: 分数}`，加和应为 1（预检会检查） |
| `flows` | 流量基准时必填 | — | `{组分: 流量}`，单位是 `total_flow_unit` |
| `total_flow` | 分数基准时必填 | — | 总流量，必须为正 |
| `total_flow_unit` | 否 | `kmol/h` | 摩尔单位：`kmol/h`、`kmol/hr`、`kgmole/h`、`kgmol/h`、`mol/h`；质量单位：`kg/h`、`kg/hr`、`kgh`、`t/h`、`ton/h`。体积单位（比如 `Nm3/h`）**一律拒绝**，因为换算需要先说明标准状态，以及它指的是哪一股物流 |
| `temperature` | 是 | — | 不能低于绝对零度（预检会检查） |
| `temperature_unit` | 否 | `C` | `C`、`degC`、`celsius`、`K`、`kelvin`、`F`、`degF` |
| `pressure` | 是 | — | 按**绝对压力**处理，不支持表压。必须为正（预检会检查） |
| `pressure_unit` | 否 | **`kPa`** | `Pa`、`kPa`、`MPa`、`bar`、`mbar`、`atm`、`psi`。默认值是 kPa，省略单位很容易出错，建议总是写明 |
| `molar_mass` | 否 | — | **不支持，写了会被预检拒绝。** HYSYS 用它自己的分子量做质量↔摩尔换算，看不到这个覆盖值，所以只要覆盖值和库值不同，模拟里的进料就会和 spec 写的质量分数**不一致且不报错**。13 个受支持组分都有库分子量，这个字段没有正当用途 |

基准和单位的组合：

| `basis` | 使用的字段 | `total_flow_unit` 要求 |
|---|---|---|
| `molar_fraction` | `fractions` + `total_flow` | 摩尔或质量单位 |
| `mass_fraction` | `fractions` + `total_flow` | 只能是质量单位 |
| `molar_flow` | `flows` | 只能是摩尔单位 |
| `mass_flow` | `flows` | 只能是质量单位 |

传给 HYSYS 的方式：组成一律换算成摩尔分数写入。`molar_flow` 基准写摩尔流量，其他基准用库里的分子量换算成质量流量再写入，由 HYSYS 自己推出摩尔流量。后面所有校验都按 HYSYS 推出的摩尔流量来计算。

### 5.4 `reactions[]`（转化反应器）

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `name` | 否 | `RXN-1`、`RXN-2`…… | 反应名称 |
| `stoichiometry` | 是 | — | `{组分: 系数}`，反应物为负。必须元素平衡，所用组分必须都在 `fluid_package.components` 里。组分名可以用第 6 节的别名 |
| `conversion_percent` | 二选一 | — | 范围 (0, 100]。写 50 表示 50%，不是 0.5 |
| `conversion_coefficients` | 二选一 | — | `[c0, c1, c2]`，按原样写入 HYSYS 的转化率关联式。用这种写法时，工具**不做**独立的转化率校验。未验证 |
| `base_component` | 实际必填 | — | 转化率的基准反应物；缺失或不支持时在预检阶段报错 |
| `phase` | 否 | `combined` | `vapour`、`liquid`、`liquid2`、`combinedLiquid`、`solid`、`combined`、`polymer`、`unknown` |

### 5.5 `reactor`

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `name` | 否 | `RX` | 反应器单元名称 |
| `kind` | 是 | — | `conversion`、`gibbs`、`equilibrium`（当前仅支持气相等温，见下表） |
| `thermal_mode` | 否 | `adiabatic` | `adiabatic` 或 `isothermal` |
| `outlet_temperature` | 等温时必填 | — | 出口温度 |
| `outlet_temperature_unit` | 否 | `C` | 同 `temperature_unit` |
| `pressure_drop_kPa` | 否 | `0` | 压降，单位 kPa |

工具自动创建、名字固定的对象：物料物流 `FEED`、`VAPOUR`、`LIQUID`，能量物流 `DUTY`，反应集 `AGENT-SET`。

### 5.6 各种组合的验证状态

| 组合 | 需要什么 | 真机状态 |
|---|---|---|
| `conversion` + `adiabatic`，单个反应 | 一个反应，带 `stoichiometry`、`conversion_percent`、`base_component` | **已验证**（甲苯歧化，转化率 50%） |
| `conversion` + `isothermal` | 同上，再加 `outlet_temperature` | 未验证 |
| `conversion`，多个反应 | | 当前拒绝：独立校验尚不支持各反应进度分解 |
| `gibbs` + `isothermal` | `components` 列全所有候选产物，`reactions` 可以为空 | **已验证**（甲烷重整 600°C 和 710°C） |
| `gibbs` + `adiabatic` | | 预检拒绝：没有经验证的热边界配置路径 |
| `gibbs`，含固体碳 | | 未验证。只验证过 `Carbon` 能加进组分表 |
| `equilibrium` | | **已接入，正式版待远程验收**。用 `LnKSource=1` 和 8 元素 ln(K) 系数数组；每次运行拟合、读回和 Q/K 校验。直接 Gibbs 来源不可写不再阻断方程来源 |

### 5.7 完整示例（转化反应器，已验证）

```json
{
  "schema": "hysys-agent/spec/1",
  "case_name": "agent-toluene",
  "fluid_package": {
    "name": "TOL-PR",
    "property_package": "PengRob",
    "components": ["Toluene", "Benzene", "o-Xylene", "m-Xylene", "p-Xylene"]
  },
  "feeds": [{
    "basis": "molar_fraction",
    "fractions": {"Toluene": 1.0},
    "total_flow": 10000.0,
    "total_flow_unit": "kg/h",
    "temperature": 380.0,
    "temperature_unit": "C",
    "pressure": 2.5,
    "pressure_unit": "MPa"
  }],
  "reactions": [{
    "name": "TOL-DISPROP",
    "stoichiometry": {"Toluene": -2.0, "Benzene": 1.0,
                      "o-Xylene": 0.3333333333333333,
                      "m-Xylene": 0.3333333333333333,
                      "p-Xylene": 0.3333333333333333},
    "conversion_percent": 50.0,
    "base_component": "Toluene",
    "phase": "combined"
  }],
  "reactor": {"name": "TOL-RX", "kind": "conversion",
              "thermal_mode": "adiabatic", "pressure_drop_kPa": 0.0},
  "assumptions": ["o/m/p 二甲苯各占三分之一是经用户确认的假设。"],
  "open_questions": []
}
```

Gibbs 反应器的完整示例，用 `python -m hysys_tools --write-examples DIR` 生成的 `methane-steam-reforming-710C.json` 即可。它的要点是：`basis` 为 `molar_flow`，`flows` 为 `{"Methane": 1000, "Water": 2700}`，`reactions` 为空，`reactor` 为 `{"kind": "gibbs", "thermal_mode": "isothermal", "outlet_temperature": 710}`。

## 6. 支持的组分

只支持下表这 13 个组分。工具内部需要每个组分的元素组成和分子量，才能做守恒校验和单位换算。表外的组分会失败，而且可能要等模拟跑完、到校验阶段才报错（见第 10 节）。

| 写进 `components` 的库名 | HYSYS 读回名 | `feeds` / `reactions` 里可用的别名 | 真机状态 |
|---|---|---|---|
| `Methane` | `Methane` | CH4 | 已验证 |
| `Water` | **`H2O`** | H2O、steam | 已验证 |
| `CO` | `CO` | carbon monoxide | 已验证 |
| `Hydrogen` | `Hydrogen` | H2 | 已验证 |
| `CO2` | `CO2` | carbon dioxide | 已验证 |
| `Toluene` | `Toluene` | C7H8、methylbenzene | 已验证 |
| `Benzene` | `Benzene` | C6H6 | 已验证 |
| `o-Xylene` | `o-Xylene` | ortho-xylene | 已验证 |
| `m-Xylene` | `m-Xylene` | meta-xylene | 已验证 |
| `p-Xylene` | `p-Xylene` | para-xylene | 已验证 |
| `Carbon` | `Carbon` | C、graphite、coal、coke、char | 已验证能加进组分表；作为固体参与 Gibbs 反应未验证 |
| `Nitrogen` | 未知 | N2 | 未验证 |
| `Oxygen` | 未知 | O2 | 未验证 |

使用规则：

- `fluid_package.components`、`feeds` 和 `reactions` 均接受受支持的别名，统一映射到库名/读回名；同一组成中不能重复写同一组分的不同别名。
- 结果里所有以组分为键的对象，键名都是 **HYSYS 读回名**，比如写 `Water` 会读回成 `H2O`。
- 别名 `coal` 会被当成纯碳。这本身就是一条建模假设，agent 用到它时必须写进 `assumptions`。
- 不要把笼统的 `xylene` 当组分用。它只在内部用于元素计算，HYSYS 组分库里没有对应项。

## 7. 结果格式

### 7.1 `status`

| 值 | 含义 |
|---|---|
| `PASS` | 案例建成、求解完成、全部校验通过、已保存并关闭 |
| `FAILED` | 中途失败，原因见 `error` 和 `error_type` |
| `CANNOT_CONNECT_TO_HYSYS` | 无法导入 COM、初始化或连接，error_type 为 environment |
| `SPEC_ERROR` | spec 文件读不进来，退出码 2；stdout 输出并尝试写 result.json |

### 7.2 PASS 时的字段

单位统一为：流量 kmol/h、质量流量 kg/h、温度 °C、压力 kPa、热负荷 kW。

| 字段 | 说明 |
|---|---|
| `schema` | `hysys-agent/result/1` |
| `status`、`started`、`finished` | 状态和起止时间 |
| `reactor_kind` | 反应器类型 |
| `assumptions`、`open_questions` | 从 spec 原样复制。**展示结果时必须一起展示给用户** |
| `warnings` | 运行通过但需要注意的事，比如案例没能关闭 |
| `case_file`、`case_file_bytes` | 保存的 `.hsc` 文件名（相对于 folder）和大小 |
| `readback_component_names` | HYSYS 读回的组分名，按组分表顺序 |
| `feed_molar_flows_kmol_h` | 进料各组分摩尔流量，按 HYSYS 推出的总摩尔流量折算 |
| `outlet.VAPOUR`、`outlet.LIQUID` | 每股产品的 `molar_flow_kmol_h`。流量大于 0 时还有 `temperature_C`、`pressure_kPa`、`mass_flow_kg_h`、`mole_fractions` |
| `component_flows_kmol_h` | 出口各组分总摩尔流量（汽相加液相） |
| `heat_duty_kW` | 外部供给反应器的热量。正值表示加热（甲烷重整吸热，读出来是正值，已验证）。绝热时为 0 |
| `solver_is_solving` | 读数时求解器是否还在运行，正常应为 `false` |
| `checks` | 独立校验结果，见 7.3 |
| `steps` | 每一步的执行记录，见 7.5 |

### 7.3 `checks`

这些数都是工具根据 HYSYS 报出的组分流量**自己重新算的**，不是从 HYSYS 读出来的。

| 字段 | 说明 |
|---|---|
| `element_relative_error`、`worst_element_relative_error` | 各元素的相对守恒误差。超过 1e-5 就判为失败 |
| `inlet_mass_kg_h`、`outlet_mass_kg_h`、`mass_relative_error` | 质量守恒，用库分子量计算。超过 1e-4 判为失败（甲苯约 1e-6，来源是库分子量和 HYSYS 分子量的差别） |
| `specified_conversion` | 仅转化反应器有。按出口重新计算每个反应的转化率，和要求值相差超过 0.05 个百分点就判为失败 |
| `reactant_conversion_percent` | 每个进料组分的转化率，键为读回名。**这是各场景的关键转化率**：重整看 `Methane`，甲苯歧化看 `Toluene` |
| `co_yield` | 只在进料含碳时出现，见下 |
| `heat_duty_kW`、`heat_duty_scope` | 热负荷，以及它的口径说明。等温时它是反应器热负荷，包含进料升温的显热，不只是反应热 |

`co_yield` 里的字段：

| 字段 | 说明 |
|---|---|
| `definition` | `(n_CO_out - n_CO_in) / n_C_feed * 100%` |
| `co_yield_percent` | 按上式计算的 CO 收率 |
| `co_produced_kmol_h`、`carbon_fed_kmol_h`、`total_carbon_out_kmol_h` | 计算用到的各个量 |
| `carbon_conversion_percent` | 只有进料含固体碳时才有数值，否则为 `null`，并附 `carbon_conversion_note` 说明原因 |
| `co_mole_fraction_dry` | 已实现：CO / (总出口摩尔流量 − 水 − 固体碳)，分母为零时为 null；仅用于该相态/物种范围适用的模型 |

### 7.4 FAILED 时的字段

除了能拿到的基本字段之外，还有：

| 字段 | 说明 |
|---|---|
| `error` | `异常类型: 消息` |
| `error_type` | `specification`、`result_check` 或 `runtime`，见第 8 节 |
| `traceback` | 完整调用栈 |
| `failed_case_file` | 另存的半成品案例文件名（如果另存成功） |
| `steps` | 最后一条是 `build_case` FAILED，之后是 `close_failed_case` 或 `left_foreign_case_alone` |

### 7.5 `steps` 的正常顺序

`create_blank_case`（出现两次：第一次标记开始尝试，第二次记录结果）→ `configure_basis` → `configure_conversion_reactions` / `gibbs_ignores_reactions`（如果有）→ `end_basis_change` → `create_streams_and_reactor`（`detail.connections` 里逐条记录每一个 COM 连接调用）→ `reactor_connected` → `release_ignored_before_solve` → `solve_and_read_outputs` → `verify_results` → `save_case` → `close_saved_case`

如果停在 `create_streams_and_reactor` 之前或之中，先看 `detail.connections` 里最后一条 FAILED 的调用。

## 8. 出错时调用方应该怎么做

先看 `error_type`，它决定该改什么：

| 情况 | 含义 | 应该怎么做 |
|---|---|---|
| 退出码 2，`status` = `SPEC_ERROR` | spec 文件本身读不进来（非法 JSON、不是对象、`schema` 前缀不对） | 修 JSON 格式 |
| `error_type` = `environment` | 跟 spec 无关的环境问题：HYSYS 连不上，或者输出目录对应的案例还开着（`new case is not blank`） | **不要改 spec。** 换一个**新的** folder 重试；连不上就请用户处理 |
| `error_type` = `specification` | spec 的内容不能执行：单位不支持、组分不认识、反应式不平衡、缺必填字段、`fractions` 和不为 1、多给了进料等 | 读 `errors` 改 spec。更省事的做法是先跑 `--validate-only` 拿到**完整**问题清单 |
| `error_type` = `result_check` | spec 已被接受，但模拟结果没通过校验，例如出口和进口完全相同（反应没有生效） | **不要改 spec。** 把 `error` 和 `failed_case_file` 报告给用户。可以换新 folder 重试一次，再失败就停下来 |
| `error_type` = `runtime` | COM 调用失败或求解超时 | 报告给用户，不要自动重试。若 `error` 里是 `KeyError`，说明有必填字段没检查到，属于工具缺陷 |
| `status` = `CANNOT_CONNECT_TO_HYSYS` | HYSYS 没开，或者有弹窗挡着 | 停止，请用户处理，不要自动重试 |
| `warnings` 非空 | 运行可能成功了，但有需要注意的地方 | 原样转告用户 |

预检报告（`--validate-only` 的输出）里 `errors` 为空就代表可以执行；`warnings` 只是提醒，不影响执行。`readback` 会给出工具层实际换算出的摩尔流量、选择的反应器类型等，方便核对理解是否一致。

## 9. 在 Python 里直接调用

```python
import pythoncom
import win32com.client
from pathlib import Path
from hysys_tools import build_case, load_spec

pythoncom.CoInitialize()
try:
    app = win32com.client.GetActiveObject('HYSYS.Application')
    result = build_case(load_spec(Path('spec.json')), Path(r'runs\20261002-120000'),
                        pythoncom, win32com, app)
finally:
    pythoncom.CoUninitialize()
```

agent 调用时更推荐用子进程跑命令行：每个案例一个独立进程，崩溃不会互相影响，而且这正是真机上验证得最多的那条路径（探针 F 组和 `Run-Tool-Layer.cmd`）。

另外两个不需要 HYSYS 的接口：`main.list_capabilities()` 返回能力说明字典；`core.feed_molar_flows(feed)` 可以离线把进料换算成摩尔流量。

## 10. 已知问题

原先这里列了 11 条，现在**全部处理完毕**（其中第 8 条是改为禁用，不是修好）。逐条留档：

| 原条目 | 现状 |
|---|---|
| 1. 混合物用 `molar_fraction` 加质量总流量时组成算错（把摩尔分数当质量分数） | **已修**：改用混合物平均摩尔质量换算。等摩尔 CH4/H2O、1000 kg/h 现在给出 29.36 / 29.36 kmol/h（摩尔分数 0.5/0.5），修复前是 31.17 / 27.75（0.529/0.471）。纯组分结果不变 |
| 2. `fractions` 和不等于 1 不报错 | **已修**：预检报错 |
| 3. `feeds` 只读第一项，多给的静默忽略 | **已修**：预检报错 |
| 4. 进料里有、`fluid_package.components` 里没有的组分不提前报错 | **已修**：预检报错 |
| 5. 所有 spec 内容错误都要等案例建好才发现 | **已修**：新增 `precheck.validate_spec` 与 `--validate-only`；`--spec` 在**连接 HYSYS 之前**先预检，所以 spec 错误不会被 `CANNOT_CONNECT` 盖住；`build_case` 内部也保留一次预检，供直接从 Python 调用的人用。spec 有问题时**不创建任何案例** |
| 6. `builder.notes` 没写进 `result.json` | **已修**：PASS 和 FAILED 都写进 `result.notes` |
| 7. `SPEC_ERROR` 不写 `result.json`；`CANNOT_CONNECT_TO_HYSYS` 没有 `error_type` | **已修**：两者都写，且都有 `error_type` |
| 8. `molar_mass` 覆盖值的键名和生效范围不一致 | **改为禁用**：见 5.3 节。这个功能本身不成立（HYSYS 用自己的分子量），不是键名问题。预检遇到就直接报错 |
| 9. `new case is not blank` 被归类为 `specification` | **已修**：新增 `core.StaleCaseError`，按**异常类型**归类为 `environment`，不再靠匹配错误文本。以后改措辞不会让分类悄悄失效 |
| 10. `co_mole_fraction_dry` 始终为 `null` | **已修**：按干基计算，分母**同时扣除水和未反应的固体碳**（只扣水会低估气化场景的干基 CO 分数），并给出 `dry_outlet_kmol_h`、`water_outlet_kmol_h` |
| 11. `--list-capabilities` 没列支持的组分和流量单位 | **已修**：新增 `components` 和 `flow_units`，并标出每种反应器的验证状态 |

本轮另外补上的检查（原先预检会放行或崩溃）：

| 情况 | 现在 |
|---|---|
| 字段类型错误（`"temperature": "hot"`、`feeds` 写成对象、系数是字符串……） | **预检本身绝不抛异常**。外层兜底把任何意外都变成一份 JSON 报告，退出码 1 不再和 traceback 混淆。已用 17 份畸形 spec 覆盖 |
| `equilibrium` 反应器 | 气相等温、完整且守恒的反应式可预检；缺目标温度、液相/固体反应拒绝。运行时拟合或 Q/K 不通过仍报错 |
| `gibbs` + `adiabatic` | **报错**。执行器对 Gibbs 反应器不设热负荷，这个组合从未验证过，早拒绝省掉一次 60 秒超时 |
| 压力 ≤ 0 | **报错** |
| 温度低于绝对零度 | **报错** |
| `molar_mass` 覆盖 | **报错**（见上表第 8 条） |
| 别名（`CH4`、`steam`、`Peng-Robinson`） | 预检和**执行**用同一个映射，所以"预检通过"和"能跑"不会矛盾 |
| Gibbs 反应器的 `components` 只有进料组分 | **警告**：没有可生成的产物，列出候选产物 |

**维护约定**：以后发现新问题就往下加；修好后标成已修并说明修复方式，不要直接删掉——留档能说明这个工具层经历过什么。每条修复都应在 `selfcheck.py` 里有对应测试（当前 110 项）。


## 12. 可靠性修复版接口补充

- 可选 `blocking_questions: list[str]` 非空时预检拒绝执行；普通 `open_questions` 继续作为说明性警告。气化示例新增流量定义、煤定义两个阻塞项。
- `case_name` 必须是 1–100 字符的 Windows 文件名主体，不含路径、保留名和尾随空格/点；已有同名 HSC 不覆盖。
- 非有限数字、重复组分别名、压降不小于进料压力、同时给定 conversion_percent 和 conversion_coefficients 均拒绝。
- `solver_evidence` 记录连续稳定次数、采样次数与检查范围。连续 3 次完整读回稳定且求解器空闲才接受；稳定性不是热力学正确性的独立证明。
- 输出温度/压力不满足目标、绝热热负荷不为零归入 result_check；无有效读回的超时归入 runtime。
- 缺少 COM 依赖也会记录结构化错误。结果 JSON 使用原子替换，写盘失败不再静默吞掉。
- `capability_combinations` 分别标识 historically_verified、experimental、unsupported；本修复版本的 remote_validation 仍为 pending。
- 推荐用 Run-Remote-Validation.cmd 验收，Run-Tool-Layer.cmd 作为兼容入口。结果有 summary.json 和完整证据 ZIP。该 runner 有互斥与总超时；直接工具 CLI 调用仍须自行串行。
