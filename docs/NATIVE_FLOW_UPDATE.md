# HYSYS 原生流量输入更新（2026-10-03）

出题人已说明：80000 Nm³/h 是煤和水混合后单股总进料，应尽量使用软件自己的单位。

## 接口

在 fraction 类型的 feed 中设置：

```json
{
  "basis": "mass_fraction",
  "fractions": {"Carbon": 0.62, "Water": 0.38},
  "total_flow": 80000,
  "total_flow_unit": "Nm3/h",
  "flow_input": "hysys",
  "flow_property": "MolarFlow",
  "temperature": 40,
  "temperature_unit": "C",
  "pressure": 40,
  "pressure_unit": "bar"
}
```

总量直接调用 `MolarFlow.SetValue(80000, 'Nm3/h')`，并用同单位 GetValue 核对；同时读取软件的 kgmole/h 和 kg/h。没有采用本地 22.414 等固定体积换算。组成仍沿用现有工具层的质量分数转摩尔分数逻辑，使用组分摩尔质量；本次不是对全套物性计算的重写。

`flow_property` 也可显式设为 `MassFlow`。不自动猜测实际体积、标准液体体积或标准气体体积之间的含义。原有未指定 flow_input 的规格保持旧路径。

注意：图形界面支持某种单位，不证明 COM 接口接受完全相同的字符串。`Nm3/h` 是待远程确认的输入字符串；若软件拒绝，就记录错误，依据该工作站的真实单位名称调整 spec，不自动切换单位或换算口径。

预检通过只表示结构、组成和输入值合法。原生单位支持、绝对流量以及固体碳 Gibbs 求解必须在 HYSYS 上验证。成功结果的 `native_flow_readback` 记录换算证据；即使后续求解失败，steps 中也会保留已成功取得的 `native_feed_flow` 读回。

## 示例及远程操作

1. 复制更新后的完整 `hysys-agent` 目录到远程工作站，使用已有 Python 环境，打开 HYSYS。
2. 运行原来的 `Run-Remote-Validation.cmd`，重新验收甲苯和两种重整工况。旧记录只代表旧版本，不能证明本次改动通过真机验收。
3. 在远程运行新增的 `Run-Native-Flow-Validation.cmd`，测试本次气化输入。它在启动它的电脑上运行，不会自动远程连接；请勿在没有 HYSYS 的开发机上执行。
4. 将 `tool-layer-runs/native-flow-*.zip` 带回。失败时先检查日志与 HYSYS 对话框，不盲目重跑。脚本超时停止 Python 工作进程，保留 HYSYS。

新示例名 `coal-slurry-gasification-native`，通过 `python -m hysys_tools --write-examples <目录>` 导出。煤按纯碳处理是明确列出的建模假设，不是出题人的新增确认；62% 为质量比例，无氧进料及外供热、固体碳支持未验证等说明仍保留。原始未澄清示例保留用于历史回归。

本次主要修改工具层。智能体目前的体积流量追问仍需后续对接：生成上述 flow_input / flow_property 字段并携带出题人澄清，不能只删阻塞问题。本次脚本直接走工具层新示例，不依赖智能体。

离线回归：`python -m unittest hysys_tools.test_native_flow hysys_tools.test_reliability`，以及 `scripts/run-all-tests.cmd`。新增测试使用假 COM 对象，不能当作真实 HYSYS 验收。
