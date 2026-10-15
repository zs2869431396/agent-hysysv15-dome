# 工具层远程验收说明

版本：`2026-10-03-equilibrium-integration-1`。最近一次真机验收为 `20261003-105341-4467d9c9`，结果 `ALL_SCENARIOS_PASS`，13 个运行文件的 SHA256 记录在 `docs/tool-acceptance-20261003-105341.json`。

本地测试不调用 HYSYS。工具层任何运行文件改动后，都要按本说明在工作站重新验收一次；智能体的能力表也会因哈希不一致而自动降级，直到新的验收记录写入。

## 你需要做什么

1. 将 ZIP 复制到远程电脑，**解压到一个新文件夹**，保留旧文件夹和历史结果作为对照。
2. 打开 HYSYS，处理已有弹窗。测试只使用自己新建的案例，不会修改或关闭你原有的案例。
3. 双击 **`Run-Remote-Validation.cmd`**。`Run-Tool-Layer.cmd` 是兼容入口，效果相同。
4. 等待脚本结束。离线检查每项最多 90 秒，健康检查最多 30 秒，单个模拟默认最多 180 秒（可用 `--timeout` 修改）。超时只停止本次 Python 工作进程，保留 HYSYS，并停止后续模拟。
5. 将窗口最后显示的 **`tool-layer-runs/acceptance-日期时间-随机编号.zip` 整个复制回来**。成功或失败都回传 ZIP。

前提沿用已验证环境：Windows、Python 3.12、pywin32、HYSYS V15，没有新增第三方依赖。若提示找不到 Python，在该目录终端执行 `python --version`，确认使用原来能运行工具层的解释器。不要同时运行其他工具脚本，也不要操作本次新建的案例。

## 脚本运行内容

1. 写出全部示例 spec、能力表 `capabilities.json`，以及 `hysys_tools/*.py` 的哈希 `source_hashes.json`。
2. 离线检查：`python -m hysys_tools.selfcheck` 和 `python -m unittest discover -s hysys_tools -t .`。任一失败就不进入 HYSYS。
3. 对每份示例做预检，结果写入 `preflight/`。原题未澄清的气化示例 `coal-slurry-gasification-unclarified` 必须预检失败，其余必须通过。
4. 健康检查，确认能连上 HYSYS。
5. 在工作站锁内串行运行六个工况，每个工况使用全新目录：

| 顺序 | 标签 | 示例 spec | 本工况的通过条件 |
|---|---|---|---|
| 1 | `toluene` | `toluene-disproportionation` | 与 `baseline_expected.json` 的历史结果在容差内一致 |
| 2 | `smr-gibbs-710C` | `methane-steam-reforming-gibbs-710C` | 与历史 Gibbs 基线一致 |
| 3 | `smr-gibbs-600C` | `methane-steam-reforming-gibbs-600C` | 与历史 Gibbs 基线一致 |
| 4 | `smr-710C` | `methane-steam-reforming-710C` | 反应器为 equilibrium，`checks.equilibrium_QK` 判定 PASS；与同温度 Gibbs 对照相比，甲烷转化率相差不超过 0.5 个百分点，热负荷相对差不超过 1% |
| 5 | `smr-600C` | `methane-steam-reforming-600C` | 同上 |
| 6 | `gasification` | `coal-slurry-gasification` | 结果含 `solid_carbon_saturation` 和 `checks.co_yield` |

每个工况还必须同时满足：工具状态 `PASS` 且退出码 0；连续稳定读数不少于 3 次且求解器空闲；保存的 `.hsc` 存在且非空；案例关闭得到确认。任一条件不满足，脚本停止后续工况并保留当前证据。

6. 收尾健康检查。全部通过时导出 `baseline_candidate.json`（本轮完整精度结果，不会覆盖历史基线），然后打包证据 ZIP。

## 怎样判断结果

| `summary.json` 的 `status` | 含义 |
|---|---|
| `ALL_SCENARIOS_PASS` | 六个工况全部通过 |
| `FAILED` | 看 `summary.json` 的 `error`，以及对应工况目录的 `result.json`、`stdout.txt`、`stderr.txt` |
| `OFFLINE_PASS_REMOTE_NOT_RUN` | 只做了离线检查，不能证明通过 HYSYS |

`summary.json` 里的 `gasification_exam_status` 是在运行工况之前写入的固定标签（`SATURATION_ROUTE_PENDING`），不随结果变化。判断气化是否通过，看 `status` 以及 `gasification` 工况记录里的 `acceptance`。

真机验收时还应打开成功的 `.hsc`，核对反应器类型、反应集、进出口条件和组成。自动回读、守恒检查和 Q/K 检查不能独立证明热力学数据在高温区完全适用。

## 历史版本

- `20261002-144131-d442afae`（版本 `2026-10-02-reliability-1`）：状态 `BASELINE_PASS_GASIFICATION_STILL_BLOCKED`。甲苯和两个 Gibbs 重整通过，气化按当时的设计被预检拒绝。这个状态值在当前脚本里已经不再使用。
- 可靠性修复版引入的保护（稳定读数要求、预检项、案例所有权检查、结果原子写入、超时只停工作进程等）在当前版本中仍然有效，清单见 [TOOL_REFERENCE.md](TOOL_REFERENCE.md) 第 10、12 节。

## 尚未覆盖的范围

1. **CSTR/PFR**：没有执行实现。
2. **Equilibrium**：只支持等温气相；液相、含固体和绝热的 Equilibrium 会被拒绝。
3. **多个 Conversion 反应**：独立校验还不能区分各反应各自的转化率，暂时拒绝。
4. **气化**：只验收了碳加水进料。煤按纯碳、Nm³ 按 0°C / 101.325 kPa 的进料总量、无氧进料（外供热）都是建模假设，报告必须保留。
5. **高温区物性**：ln(K) 拟合准确和 Q/K 接近 1 只说明计算自洽，不能证明 HYSYS 的 Gibbs 数据在该温区本身准确。
6. **并发**：验证脚本之间有互斥；直接调用 `python -m hysys_tools` 的其他程序需要自行串行。智能体的适配器有自己的工作站锁。
7. **智能体端到端**：本流程直接调用工具层，不经过智能体。智能体在工作站上的端到端验证用 `Run-Agent-On-Workstation.cmd`。

## 不连接 HYSYS 的检查

双击 `Run-Offline-Checks.cmd`，或在项目目录执行：

```powershell
python -m hysys_tools.remote_check --offline
```

这也会生成 ZIP，但状态明确标识为离线。CLI 仍支持：

```powershell
python -m hysys_tools --list-capabilities
python -m hysys_tools --validate-only --spec path\to\spec.json
```
