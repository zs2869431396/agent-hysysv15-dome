# Equilibrium 正式接入（2026-10-03）

> **验收结论**：本版本 `2026-10-03-equilibrium-integration-1` 已在远程验收 `20261003-105341-4467d9c9` 中通过，`summary.json.status` 为 `ALL_SCENARIOS_PASS`，下文六个工况全部 PASS。13 个运行文件的 SHA256 记录在 `docs/tool-acceptance-20261003-105341.json`。验收流程与判读见 [REMOTE_VALIDATION.md](REMOTE_VALIDATION.md)。

## 这次改了什么

源码主目录是 `D:\BiShi\hysys-agent`。先合入远程返回目录中已通过气化测试的 saturation 工具层，再接入 Equilibrium。远程返回的原始证据和备份目录没有被改写。

- 重整默认工具示例用 Equilibrium，710℃、600℃各一个工况；另外保留两个 Gibbs 对照工况。
- 气化主示例使用已经单独通过工具测试的 `solid_carbon=saturation` 路径，正式进入主验收，不再把“拒绝输入”算作气化通过。
- `LnKSource=1`，分压基准 bar，气相反应，温度接近值为零；通过 `EquilibriumConstantParameterArrayValue` 写入 8 元素 VARIANT。
- 从每个反应的化学计量和软件 EvaluateGibbs 通用计算 ΔG(T)，按已验证的 1 atm 参考态转到 bar。没有写死 SMR/WGS 的 K 或出口结果。
- 每个出口温度附近 ±150 K 取 8 点，拟合 A+B/T+C·ln(T)；检查训练点之间及目标温度处，最大 ln(K) 残差不得超过 0.005。
- 配置及求解后均核对来源、分压基准、相态和全部系数；最终用实际出口气相组成、温度、压力检查每个反应 `abs(ln(Q/K)) < 0.05`。缺证据或不满足条件不能 PASS。
- 当前只开放等温、气相 Equilibrium；液相、固体反应以及绝热 Equilibrium 仍拒绝，不暗中改用其他模型。

## 怎么运行

将新版 ZIP **解压到远程的新目录**，使用原有 Python/pywin32 环境，启动 HYSYS。保管好自己的案例，不要同时运行其他模拟脚本。双击 `Run-Remote-Validation.cmd`。

脚本先执行离线测试，然后顺序运行：甲苯、两个历史 Gibbs 对照、两个新 Equilibrium 工况、饱和碳气化。超时或任一验收失败会停止后续工况。结束后返回 `tool-layer-runs/acceptance-*.zip`。

真正全通过时 `summary.json.status` 为 **ALL_SCENARIOS_PASS**。重整结果中必须包含 `checks.equilibrium_QK` 和 `equilibrium_evidence`；气化必须含饱和碳、CO 收率、守恒、固体去向与能量检查。

## 基线如何处理

`baseline_expected.json` 保留历史甲苯/Gibbs 原始基线，继续严格比对。Equilibrium 对本轮 Gibbs 的交叉检查限值：甲烷转化率差不超过 0.5 个百分点、热负荷相对差不超过 1%，同时必须通过自己的 Q/K 和守恒检查。这些是不同模型的交叉检查限值，不要求逐位相同。

只有所有工况和收尾健康检查通过，脚本才导出 `baseline_candidate.json`，来自本轮真实结果的完整精度数值。它不会自动覆盖历史基线，也不会用聊天里四舍五入的 54.17%、30.49% 充当新答案。

## 证据与剩余边界

探针七原始 JSON 确认了两个温度都由 `lnk_equation_array` 求解成功；直接 Gibbs 来源仍未生效。这套“通用拟合＋正式结果门控＋六工况验收”的代码已在验收 `20261003-105341-4467d9c9` 中真机通过。

旧警告中 25/426.85 的温度上限没有可靠单位证据，已去掉直接标作 K 的断言，也没有未经验证改成 ℃。数值拟合准确和 Q/K 接近 1 都不能证明基础 Gibbs 数据在外推温区绝对准确。

气化沿用最新已测试的纯碳、标况及外供热等假设，报告必须保留。它是 Conversion 配合气相 Gibbs、外层求解石墨饱和的组合流程，并非单台直接使用库 Carbon 的 Gibbs 反应器。

上述验收直接调用工具层完成，不经过智能体。`reactor_agent` 已随后同步新能力：重整按“反应网络闭合”选 Equilibrium，气化以带默认答案的追问确认 Nm³ 基准和“煤按纯碳”后走饱和碳路线，能力表按本次验收的文件哈希判定 verified。改动记录见 `docs/AGENT_FIX_NOTES.md`。智能体端到端仍需在工作站上用 `Run-Agent-On-Workstation.cmd` 跑一次，跑完之前不要把工具层验收说成智能体端到端验收。
