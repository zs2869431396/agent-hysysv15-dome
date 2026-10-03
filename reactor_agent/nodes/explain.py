"""The `explain` node: turn the run's record into something a person can read.

Three things this must do, all of them asked for by the exam or by a defect that was
found by its absence:

**Report the computed numbers.** The exam requires the simulation results, and an
earlier version returned only statuses and elapsed times. The results come from
`ExecutionResult.results()` and every value is printed with its unit.

**Compare operating cases side by side.** Scenario 2 asks for the distribution at two
outlet temperatures; two separate blocks of text make the comparison the reader's
problem, so a table is produced instead.

**Declare what we chose ourselves.** Values this system picked - the reformer feed
flow, the equal split of the xylene isomers, the thermal boundary when the request
did not state one - are indistinguishable from extracted values once they are in the
spec. A reader who cannot tell which numbers came from them and which from us cannot
judge the result, so they are listed explicitly.

Nothing here computes anything. It formats what the tool layer already validated.
"""
from __future__ import annotations

from typing import Any

from ..pipeline import READY, UNSUPPORTED
from .state import AgentState

# Which species to show in the comparison table. Percentages of everything would be
# unreadable, so the ones an engineer actually compares are listed first and the rest
# are appended in the order the tool layer reported them.
_PREFERRED_SPECIES = ('Methane', 'CH4', 'H2O', 'Water', 'CO', 'CO2',
                      'Hydrogen', 'H2', 'Toluene', 'Benzene')


def _fmt(value: Any, unit: str = '', digits: int = 4) -> str:
    if value is None:
        return '未报告'
    if isinstance(value, (int, float)):
        return '%.*f%s' % (digits, value, (' ' + unit) if unit else '')
    return str(value)


def _num(value: Any, digits: int = 4) -> str:
    if isinstance(value, (int, float)):
        return '%.*f' % (digits, value)
    return '未报告'


def _fmt_signed(value: Any, digits: int = 2) -> str:
    if isinstance(value, (int, float)):
        return '%.*e' % (digits, value)
    return '未报告'


def _percent_map(mapping: dict[str, Any], digits: int = 4) -> str:
    if not mapping:
        return '未报告'
    return '，'.join('%s %s%%' % (name, _num(value, digits))
                    for name, value in mapping.items())


def _outlet_of(execution: dict[str, Any]) -> dict[str, Any]:
    """The stream that actually leaves the reactor.

    Not the first one alphabetically: the tool layer reports every outlet stream, and
    a vapour-liquid flash produces an empty LIQUID alongside the real VAPOUR. Taking
    the first would put zeros in the comparison table and silently drop every row
    that depended on a temperature or a flow.
    """
    streams = (execution.get('results') or {}).get('streams') or {}
    best: dict[str, Any] = {}
    best_flow = -1.0
    for name in sorted(streams):
        stream = streams[name]
        flow = stream.get('molar_flow_kmol_h')
        flow = float(flow) if isinstance(flow, (int, float)) else 0.0
        if flow > best_flow:
            best, best_flow = stream, flow
    return best


def _case_block(execution: dict[str, Any]) -> list[str]:
    results = execution.get('results') or {}
    lines = ['工况 %s' % execution.get('case_id')]
    if execution.get('status') != 'PASS':
        lines.append('  状态 %s' % execution.get('status'))
        if execution.get('error'):
            lines.append('  错误 %s' % execution['error'])
        return lines

    streams = results.get('streams') or {}
    outlet = _outlet_of(execution)
    # A phase with no flow is a flash result, not a product; showing it as a block of
    # "not reported" lines only hides the stream that matters.
    shown = [outlet] if outlet else []
    shown += [s for name, s in sorted(streams.items())
              if s is not outlet
              and isinstance(s.get('molar_flow_kmol_h'), (int, float))
              and abs(s['molar_flow_kmol_h']) > 1e-9]
    for stream in shown:
        lines.append('  出口流股 %s' % stream.get('name', ''))
        lines.append('    温度 %s   压力 %s'
                     % (_fmt(stream.get('temperature_C'), '°C', 2),
                        _fmt(stream.get('pressure_kPa'), 'kPa', 2)))
        lines.append('    摩尔流量 %s   质量流量 %s'
                     % (_fmt(stream.get('molar_flow_kmol_h'), 'kmol/h', 4),
                        _fmt(stream.get('mass_flow_kg_h'), 'kg/h', 2)))
        fractions = stream.get('mole_fractions') or {}
        if fractions:
            ordered = sorted(fractions.items(),
                             key=lambda kv: -abs(kv[1] if isinstance(kv[1], (int, float)) else 0))
            lines.append('    摩尔组成 %s'
                         % '，'.join('%s %.4f%%' % (k, 100.0 * v)
                                    for k, v in ordered))

    conversions = results.get('reactant_conversion_percent') or {}
    if conversions:
        lines.append('  转化率 %s' % _percent_map(conversions))
    if results.get('heat_duty_kW') is not None:
        lines.append('  热负荷 %s kW' % _num(results['heat_duty_kW'], 4))

    co_yield = results.get('co_yield')
    if isinstance(co_yield, dict) and co_yield.get('co_yield_percent') is not None:
        lines.append('  CO 收率 %s%%' % _num(co_yield['co_yield_percent'], 4))
        if co_yield.get('definition'):
            lines.append('    计算口径 %s' % co_yield['definition'])
        if co_yield.get('dry_outlet_kmol_h') is not None:
            lines.append('    干基出口 %s kmol/h，干基 CO 摩尔分数 %s%%'
                         % (_num(co_yield['dry_outlet_kmol_h'], 4),
                            _num(100.0 * (co_yield.get('co_mole_fraction_dry') or 0), 4)))

    checks = []
    if results.get('worst_element_relative_error') is not None:
        checks.append('元素守恒最大相对误差 %s'
                      % _fmt_signed(results['worst_element_relative_error']))
    if results.get('mass_relative_error') is not None:
        checks.append('质量相对误差 %s' % _fmt_signed(results['mass_relative_error']))
    if results.get('solver_is_solving') is not None:
        checks.append('求解器状态 %s' % ('收敛' if not results['solver_is_solving']
                                        else '仍在计算'))
    if checks:
        lines.append('  校验 %s' % '；'.join(checks))
    if results.get('case_file'):
        lines.append('  案例文件 %s' % results['case_file'])
    return lines


def _comparison_table(executions: list[dict[str, Any]]) -> list[str]:
    """A side-by-side table when there is more than one operating case."""
    usable = [e for e in executions if (e.get('results') or {}).get('streams')]
    if len(usable) < 2:
        return []

    def outlet_of(execution: dict[str, Any]) -> dict[str, Any]:
        return _outlet_of(execution)

    species: list[str] = []
    for execution in usable:
        for name in (outlet_of(execution).get('mole_fractions') or {}):
            if name not in species:
                species.append(name)
    species.sort(key=lambda name: (name not in _PREFERRED_SPECIES,
                                   _PREFERRED_SPECIES.index(name)
                                   if name in _PREFERRED_SPECIES else 0, name))

    headers = ['指标'] + [str(e.get('case_id')) for e in usable]
    rows: list[list[str]] = [headers]

    def add(label: str, getter) -> None:
        values = [getter(e) for e in usable]
        if any(value != '未报告' for value in values):
            rows.append([label] + values)

    add('出口温度 °C', lambda e: _num(outlet_of(e).get('temperature_C'), 2))
    add('出口压力 kPa', lambda e: _num(outlet_of(e).get('pressure_kPa'), 2))
    add('摩尔流量 kmol/h', lambda e: _num(outlet_of(e).get('molar_flow_kmol_h'), 4))
    add('质量流量 kg/h', lambda e: _num(outlet_of(e).get('mass_flow_kg_h'), 2))
    for name in species:
        add('%s %%' % name,
            lambda e, n=name: _num(100.0 * ((outlet_of(e).get('mole_fractions')
                                             or {}).get(n) or 0.0), 4))
    conversions = sorted({name for e in usable
                          for name in ((e.get('results') or {}
                                        ).get('reactant_conversion_percent') or {})})
    for name in conversions:
        add('%s 转化率 %%' % name,
            lambda e, n=name: _num(((e.get('results') or {}
                                     ).get('reactant_conversion_percent')
                                    or {}).get(n), 4))
    add('热负荷 kW', lambda e: _num((e.get('results') or {}).get('heat_duty_kW'), 2))

    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    out = ['工况对比', '  ' + '  '.join(cell.ljust(widths[i])
                                       for i, cell in enumerate(rows[0]))]
    out.append('  ' + '  '.join('-' * widths[i] for i in range(len(headers))))
    for row in rows[1:]:
        out.append('  ' + '  '.join(cell.ljust(widths[i])
                                    for i, cell in enumerate(row)))
    return out


def explain_node(state: AgentState) -> dict[str, Any]:
    """Compose the human-facing explanation from what the run actually established."""
    decision = state.get('decision') or {}
    lines: list[str] = []

    if state.get('status') == UNSUPPORTED:
        lines.append('选型结论：%s（%s）。工具层无法执行该组合，因此没有运行模拟。'
                     % (decision.get('preferred'), decision.get('capability')))
        if decision.get('explanation'):
            lines.append(decision['explanation'])
        return {'explanation': '\n'.join(lines)}

    lines.append('反应器选型：%s（能力状态 %s，规则 %s）'
                 % (decision.get('executed'), decision.get('capability'),
                    decision.get('rule')))
    if decision.get('substituted'):
        lines.append('注意：理论选型为 %s，实际执行为 %s。原因：%s'
                     % (decision.get('preferred'), decision.get('executed'),
                        decision.get('fallback_reason') or '见能力表'))
    if decision.get('explanation'):
        lines.append(decision['explanation'])

    if state.get('blocking'):
        lines.append('')
        lines.append('以下信息不足，未执行任何模拟：')
        lines.extend('  - %s' % item['question'] for item in state['blocking'])

    if state.get('assumptions'):
        mine = [a for a in state['assumptions'] if a.get('source') == 'agent_default']
        if mine:
            lines.append('')
            lines.append('以下数值或建模条件由本系统选定（非用户给定），报告中必须声明：')
            lines.extend('  - %s = %s（%s）' % (a['field'], a['value'], a['scope'])
                         for a in mine)

    executions = state.get('executions') or []
    if executions:
        lines.append('')
        lines.append('计算结果')
        lines.append('=' * 64)
        for execution in executions:
            lines.extend(_case_block(execution))
            lines.append('')
        table = _comparison_table(executions)
        if table:
            lines.extend(table)
    elif state.get('status') == READY:
        lines.append('')
        lines.append('规格已编译并通过预检，尚未运行模拟（dry run）。')

    # Problems that did not stop the run are still worth stating.
    if state.get('problems'):
        lines.append('')
        lines.append('运行记录：')
        lines.extend('  - %s' % problem for problem in state['problems'])

    return {'explanation': '\n'.join(lines)}
