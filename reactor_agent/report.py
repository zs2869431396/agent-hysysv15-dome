"""The one place the human-facing report is written.

Both callers use this module: the graph's `explain` node (through `view_from_state`)
and the single-pass `pipeline` (through `view_from_run`). They used to write their own
text, so the same run was described differently depending on which path produced it -
a pipeline dry run said "已完成选型与规格编译" while the graph said "规格已编译并通过
预检", and only one of them mentioned the substitution or the assumptions at all.

Two rules, both of them the project's:

  * **No simulation is performed here.** Component flows and the oxygen-balance
    ceiling are explicit arithmetic over validated results, labelled as derived.
  * **Nothing is hidden.** Sections are omitted only when they are empty; a check that
    the tool layer ran is printed with its verdict, including when the verdict is bad.

Text is Chinese; status codes and field names stay English.
"""
from __future__ import annotations

import math
from typing import Any

from hysys_tools.core import atoms_of, canonical

from .adapters.hysys_cli import ExecutionResult

# Status values, repeated here rather than imported from `pipeline`: `pipeline`
# imports this module, so importing back would be a cycle.
READY = 'READY'
UNSUPPORTED = 'UNSUPPORTED'

# A stream this rich in carbon is the solid-carbon phase the saturation route leaves
# behind, not a product stream.
_SOLID_CARBON_FRACTION = 0.999

# Which species to show in the comparison table, in the order an engineer reads them.
_PREFERRED_SPECIES = ('Methane', 'CH4', 'H2O', 'Water', 'CO', 'CO2',
                      'Hydrogen', 'H2', 'Toluene', 'Benzene')

# The saturated-carbon route leaves unreacted carbon in a stream HYSYS names LIQUID.
_SOLID_STREAM_NOTE = '（固相碳；HYSYS 物流名为 LIQUID，并非液态碳）'

_ISOTHERMAL_SCOPE = ('维持出口温度所需的外部热量，包含进料从入口温度升温的显热和'
                     '反应热，不只是反应热')


# --------------------------------------------------------------------- helpers

def _fmt(value: Any, unit: str = '', digits: int = 4) -> str:
    if value is None:
        return '未报告'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '%.*f%s' % (digits, value, (' ' + unit) if unit else '')
    return str(value)


def _num(value: Any, digits: int = 4) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '%.*f' % (digits, value)
    return '未报告'


def _fmt_signed(value: Any, digits: int = 2) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '%.*e' % (digits, value)
    return '未报告'


def _percent_map(mapping: dict[str, Any], digits: int = 4) -> str:
    if not mapping:
        return '未报告'
    return '，'.join('%s %s%%' % (name, _num(value, digits))
                    for name, value in mapping.items())


def _pick(mapping: dict[str, Any] | None, *names: str) -> Any:
    """First present value among several spellings of one quantity."""
    for name in names:
        if isinstance(mapping, dict) and mapping.get(name) is not None:
            return mapping[name]
    return None


def _fraction_of(stream: dict[str, Any], name: str) -> float:
    fractions = stream.get('mole_fractions') or {}
    value = _pick(fractions, name, 'CH4' if name == 'Methane' else name)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _is_solid_carbon_stream(stream: dict[str, Any]) -> bool:
    return _fraction_of(stream, 'Carbon') >= _SOLID_CARBON_FRACTION


def _outlet_of(execution: dict[str, Any],
               want_all: bool = False) -> dict[str, Any] | list[dict[str, Any]]:
    """The stream (or streams) that actually leave the reactor.

    Not the first one alphabetically: a vapour-liquid flash produces an empty LIQUID
    alongside the real VAPOUR, and the saturated-carbon route produces a LIQUID that is
    nearly pure solid carbon. `want_all=False` (the default) returns the single main
    outlet, skipping solid-carbon streams; `want_all=True` returns every stream with a
    real flow, main outlet first.
    """
    streams = (execution.get('results') or {}).get('streams') or {}
    real: list[dict[str, Any]] = [
        stream for _, stream in sorted(streams.items())
        if isinstance(stream.get('molar_flow_kmol_h'), (int, float))
        and abs(stream['molar_flow_kmol_h']) > 1e-9]

    candidates = [s for s in real if not _is_solid_carbon_stream(s)] or real
    main: dict[str, Any] = {}
    best_flow = -1.0
    for stream in candidates:
        flow = float(stream['molar_flow_kmol_h'])
        if flow > best_flow:
            main, best_flow = stream, flow

    if not want_all:
        return main
    return ([main] if main else []) + [s for s in real if s is not main]


def _stream_title(stream: dict[str, Any]) -> str:
    name = stream.get('name', '')
    if _is_solid_carbon_stream(stream):
        return '%s%s' % (name, _SOLID_STREAM_NOTE)
    return str(name)


# ------------------------------------------------------------------ the report

def render_report(view: dict[str, Any]) -> str:
    """Render one run. Missing sections are omitted, never faked."""
    decision = view.get('decision') or {}
    status = view.get('status')
    lines: list[str] = []

    # ---------------------------------------------------------------- 1 选型
    if status == UNSUPPORTED:
        lines.append('选型结论：%s（%s）。工具层无法执行该组合，因此没有运行模拟。'
                     % (decision.get('preferred'), decision.get('capability')))
        if decision.get('explanation'):
            lines.append(decision['explanation'])
        if view.get('problems'):
            lines.append('')
            lines.append('运行记录：')
            lines.extend('  - %s' % problem for problem in view['problems'])
        return '\n'.join(lines)

    lines.append('反应器选型：%s（能力状态 %s，规则 %s）'
                 % (decision.get('executed') or decision.get('preferred'),
                    decision.get('capability'), decision.get('rule')))
    if decision.get('substituted'):
        lines.append('注意：理论选型为 %s，实际执行为 %s。原因：%s'
                     % (decision.get('preferred'), decision.get('executed'),
                        decision.get('fallback_reason') or '见能力表'))
    if decision.get('explanation'):
        lines.append(decision['explanation'])

    # ------------------------------------------------------------ 2 待确认
    blocking = view.get('blocking') or []
    if blocking:
        lines.append('')
        lines.append('待确认问题（未执行任何模拟）：')
        for item in blocking:
            text = '  - %s' % item.get('question')
            if item.get('default'):
                text += '（默认：%s）' % item['default']
            lines.append(text)

    open_questions = view.get('open_questions') or []
    if open_questions:
        lines.append('')
        lines.append('提示（不阻塞运行）：')
        lines.extend('  - %s' % text for text in open_questions)

    # -------------------------------------------------------------- 3 假设
    assumptions = view.get('assumptions') or []
    groups = (('agent_default', '本系统选定（非用户给定）'),
              ('user_answer', '用户确认'),
              ('derived', '推导所得'))
    if assumptions:
        lines.append('')
        lines.append('假设与取值来源：')
        for source, title in groups:
            members = [a for a in assumptions if a.get('source') == source]
            if source == 'user_text':
                continue
            if not members:
                continue
            lines.append('  %s：' % title)
            for assumption in members:
                lines.append('    - %s = %s（%s）'
                             % (assumption.get('field'), assumption.get('value'),
                                assumption.get('scope')))
        stated = [a for a in assumptions if a.get('source') == 'user_text']
        if stated:
            lines.append('  来自题目原文：')
            for assumption in stated:
                lines.append('    - %s = %s（%s）'
                             % (assumption.get('field'), assumption.get('value'),
                                assumption.get('scope')))

    # -------------------------------------------------------------- 4 结果
    executions = view.get('executions') or []
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
            lines.append('')
            lines.extend(_temperature_trend(executions))
    elif status == READY:
        lines.append('')
        lines.append('规格已编译并通过预检，尚未运行模拟（dry run）。')

    # ---------------------------------------------------------- 7 运行记录
    if view.get('problems'):
        lines.append('')
        lines.append('运行记录：')
        lines.extend('  - %s' % problem for problem in view['problems'])

    return '\n'.join(lines)


def _case_block(execution: dict[str, Any]) -> list[str]:
    results = execution.get('results') or {}
    lines = ['工况 %s' % execution.get('case_id')]
    if execution.get('status') != 'PASS':
        lines.append('  状态 %s' % execution.get('status'))
        if execution.get('error'):
            lines.append('  错误 %s' % execution['error'])
        return lines

    for stream in _outlet_of(execution, want_all=True):
        lines.append('  出口流股 %s' % _stream_title(stream))
        lines.append('    温度 %s   压力 %s'
                     % (_fmt(stream.get('temperature_C'), '°C', 2),
                        _fmt(stream.get('pressure_kPa'), 'kPa', 2)))
        lines.append('    摩尔流量 %s   质量流量 %s'
                     % (_fmt(stream.get('molar_flow_kmol_h'), 'kmol/h', 4),
                        _fmt(stream.get('mass_flow_kg_h'), 'kg/h', 2)))
        fractions = stream.get('mole_fractions') or {}
        if fractions:
            ordered = sorted(
                fractions.items(),
                key=lambda kv: -abs(kv[1] if isinstance(kv[1], (int, float)) else 0))
            lines.append('    摩尔组成 %s'
                         % '，'.join('%s %.4f%%' % (k, 100.0 * v)
                                    for k, v in ordered))
            total = stream.get('molar_flow_kmol_h')
            if isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total):
                lines.append('    各组分摩尔流量（kmol/h，由本流股总摩尔流量 × 摩尔分数推算）：')
                for name, fraction in ordered:
                    value = (total * fraction if isinstance(fraction, (int, float))
                             and not isinstance(fraction, bool) and math.isfinite(fraction) else None)
                    lines.append('      %s：%s' % (name, _fmt(value, digits=4)))

    conversions = results.get('reactant_conversion_percent') or {}
    if conversions:
        lines.append('  转化率 %s' % _percent_map(conversions))

    if results.get('heat_duty_kW') is not None:
        lines.append('  热负荷 %s kW' % _num(results['heat_duty_kW'], 4))
        scope = results.get('heat_duty_scope')
        if isinstance(scope, str) and scope.strip():
            lines.append('    热负荷口径 %s'
                         % ('绝热：热负荷为 0，出口温度为计算结果'
                            if scope.startswith('Adiabatic') else _ISOTHERMAL_SCOPE))

    lines.extend(_equilibrium_lines(results))
    lines.extend(_co_yield_lines(results))
    lines.extend(_saturation_lines(results))

    checks = []
    if results.get('worst_element_relative_error') is not None:
        checks.append('元素守恒最大相对误差 %s'
                      % _fmt_signed(results['worst_element_relative_error']))
    if results.get('mass_relative_error') is not None:
        checks.append('质量相对误差 %s'
                      % _fmt_signed(results['mass_relative_error']))
    independent = results.get('independent_duty') or {}
    if independent.get('verdict'):
        checks.append('独立热负荷偏差 %s（%s）'
                      % (_fmt_signed(independent.get('relative_deviation'), 2),
                         independent['verdict']))
    gibbs = results.get('gibbs_equilibrium') or {}
    if gibbs.get('verdict'):
        checks.append('Gibbs 平衡判定 %s（与平衡态的偏离量级 %s）'
                      % (gibbs['verdict'],
                         _num(gibbs.get('orders_from_equilibrium'), 5)))
    if results.get('solver_is_solving') is not None:
        checks.append('求解器状态 %s'
                      % ('收敛' if not results['solver_is_solving'] else '仍在计算'))
    if results.get('condensed_phase_location'):
        checks.append('凝相位置 %s' % results['condensed_phase_location'])
    if checks:
        lines.append('  校验 %s' % '；'.join(checks))

    if results.get('normal_volume_conversion'):
        conversion = results['normal_volume_conversion']
        if isinstance(conversion, dict):
            detail = '，'.join('%s %s' % (key, _fmt(value, digits=4))
                              for key, value in conversion.items())
        elif isinstance(conversion, str):
            detail = conversion
        else:
            detail = '未报告（标准体积换算字段类型异常）'
        lines.append('  标准体积换算：%s'
                     % detail)

    if results.get('warnings'):
        lines.append('  工具层提示：')
        lines.extend('    - %s' % warning for warning in results['warnings'])

    if results.get('case_file'):
        lines.append('  案例文件 %s' % results['case_file'])
    return lines


def _equilibrium_lines(results: dict[str, Any]) -> list[str]:
    """One line per reaction: Q/K and its verdict, plus the ln K fit residual."""
    lines: list[str] = []
    qk = results.get('equilibrium_QK') or {}
    residuals = {item.get('reaction'): item
                 for item in (results.get('equilibrium_fit') or [])
                 if isinstance(item, dict)}
    for reaction in (qk.get('reactions') or []):
        if not isinstance(reaction, dict):
            continue
        name = reaction.get('reaction')
        lines.append('  %s：Q/K = %s，|ln(Q/K)| = %s，判定 %s'
                     % (name, _num(reaction.get('Q_over_K'), 4),
                        _num(abs(reaction['ln_Q_over_K']), 4)
                        if isinstance(reaction.get('ln_Q_over_K'), (int, float))
                        else '未报告',
                        reaction.get('verdict')))
        fit = residuals.get(name) or {}
        if fit.get('fit_max_residual') is not None:
            lines.append('    平衡常数拟合最大残差 %s'
                         % _fmt_signed(fit['fit_max_residual']))
    return lines


def _co_yield_lines(results: dict[str, Any]) -> list[str]:
    co_yield = results.get('co_yield')
    if not isinstance(co_yield, dict) or co_yield.get('co_yield_percent') is None:
        return []
    lines = ['  CO 收率 %s%%' % _num(co_yield['co_yield_percent'], 4)]
    if co_yield.get('definition'):
        lines.append('    计算口径 %s' % co_yield['definition'])
    if co_yield.get('dry_outlet_kmol_h') is not None:
        lines.append('    干基出口 %s kmol/h，干基 CO 摩尔分数 %s%%'
                     % (_num(co_yield['dry_outlet_kmol_h'], 4),
                        _num(100.0 * (co_yield.get('co_mole_fraction_dry') or 0), 4)))
    if co_yield.get('carbon_conversion_percent') is not None:
        lines.append('    碳转化率 %s%%'
                     % _num(co_yield['carbon_conversion_percent'], 4))

    ceiling = _oxygen_balance_ceiling(results)
    if ceiling is not None:
        percent, oxygen_flow, carbon_flow = ceiling
        lines.append('    氧平衡上限 %s%%（进料 O 原子流量 %s kmol/h ÷ C 原子流量 %s '
                     'kmol/h；每个 CO 分子需要一个 O，氧只来自水）'
                     % (_num(percent, 2), _num(oxygen_flow, 4),
                        _num(carbon_flow, 4)))
        if co_yield['co_yield_percent'] and percent:
            ratio = co_yield['co_yield_percent'] / percent
            lines.append('    CO 收率与上限之比 %s' % _num(ratio, 3))
            if ratio >= 0.9:
                lines.append('    CO 收率主要受进料中的水量限制')
    return lines


def _oxygen_balance_ceiling(results: dict[str, Any]
                            ) -> tuple[float, float, float] | None:
    """CO yield ceiling from the feed's oxygen balance, as a percentage.

    Every CO molecule needs one oxygen atom, and here the only oxygen carrier in the
    feed is water, so n_CO <= n_O/2 and the ceiling is 100 * (O/C). Only reported when
    that is actually true of the feed: with an oxygen feed the ceiling would be a
    different number and quoting this one would be wrong.
    """
    flows = results.get('feed_molar_flows_kmol_h')
    if not isinstance(flows, dict) or not flows:
        return None
    oxygen_carriers: list[str] = []
    oxygen_flow = 0.0
    carbon_flow = 0.0
    for name, flow in flows.items():
        if not isinstance(flow, (int, float)) or flow <= 0:
            continue
        try:
            atoms = atoms_of(name)
        except Exception:                               # noqa: BLE001
            return None
        if 'O' in atoms:
            oxygen_carriers.append(canonical(name))
            oxygen_flow += float(atoms['O']) * flow
        if 'C' in atoms:
            carbon_flow += float(atoms['C']) * flow
    # Only water carries oxygen, so the ceiling is 100 * (O/C). With an oxygen feed it
    # would be a different number, and quoting this one would be wrong.
    if oxygen_carriers != ['water'] or carbon_flow <= 0:
        return None
    return 100.0 * oxygen_flow / carbon_flow, oxygen_flow, carbon_flow


def _saturation_lines(results: dict[str, Any]) -> list[str]:
    saturation = results.get('solid_carbon_saturation')
    if not isinstance(saturation, dict):
        return []
    lines: list[str] = []
    if saturation.get('carbon_conversion_x') is not None:
        lines.append('  饱和碳路线：碳转化率 X = %s，水量允许的上限 %s'
                     % (_num(saturation['carbon_conversion_x'], 6),
                        _num(saturation.get('water_limited_x_max'), 6)))
        activity = saturation.get('carbon_activity') or {}
        lines.append('    三条途径的碳活度：methanation %s，water-gas %s，'
                     'boudouard %s（跨度 %s 个数量级）'
                     % (_num(activity.get('via_methanation'), 6),
                        _num(activity.get('via_water_gas'), 6),
                        _num(activity.get('via_boudouard'), 6),
                        _num(activity.get('spread_decades'), 6)))
    duty = saturation.get('duty_by_reactor_kW') or {}
    if duty:
        lines.append('    分反应器热负荷 kW：%s'
                     % '，'.join('%s %s' % (k, _num(v, 2))
                                for k, v in duty.items()))
    if saturation.get('library_carbon_gibbs_used') is not None:
        lines.append('    使用 HYSYS 库 Carbon 的 Gibbs 数据：%s'
                     % ('是' if saturation['library_carbon_gibbs_used'] else '否'))
    return lines


def _comparison_table(executions: list[dict[str, Any]]) -> list[str]:
    """A side-by-side table when there is more than one operating case."""
    usable = [e for e in executions if (e.get('results') or {}).get('streams')]
    if len(usable) < 2:
        return []

    species: list[str] = []
    for execution in usable:
        outlet = _outlet_of(execution)
        for name in (outlet.get('mole_fractions') or {}):
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

    add('出口温度 °C', lambda e: _num(_outlet_of(e).get('temperature_C'), 2))
    add('出口压力 kPa', lambda e: _num(_outlet_of(e).get('pressure_kPa'), 2))
    add('摩尔流量 kmol/h', lambda e: _num(_outlet_of(e).get('molar_flow_kmol_h'), 4))
    add('质量流量 kg/h', lambda e: _num(_outlet_of(e).get('mass_flow_kg_h'), 2))
    for name in species:
        add('%s %%' % name,
            lambda e, n=name: _num(100.0 * _fraction_of(_outlet_of(e), n), 4))
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


def _temperature_trend(executions: list[dict[str, Any]]) -> list[str]:
    """What changed between the coldest and the hottest case, and why.

    Two separate conditions produce the mechanistic sentence, and each is read from
    the numbers rather than assumed: the conversion has to rise with temperature for
    the endothermic-reforming explanation to apply, and the CO/CO2 ratio has to rise
    for the water-gas-shift explanation to apply. When a direction is opposite to the
    general rule the text says so instead of asserting the rule.
    """
    usable = [e for e in executions if (e.get('results') or {}).get('streams')]
    if len(usable) < 2:
        return []
    ordered = sorted(usable, key=lambda e: _outlet_of(e).get('temperature_C') or 0.0)
    cold, hot = ordered[0], ordered[-1]
    if cold is hot:
        return []

    def component_flow(execution: dict[str, Any], *names: str) -> float | None:
        flows = (execution.get('results') or {}).get('component_flows_kmol_h') or {}
        return _pick(flows, *names)

    def ratio(execution: dict[str, Any], numerator: str,
              denominator: str) -> float | None:
        top = component_flow(execution, numerator)
        bottom = component_flow(execution, denominator)
        if top is None or bottom in (None, 0):
            return None
        return top / bottom

    cold_conversion = _pick(
        (cold.get('results') or {}).get('reactant_conversion_percent') or {},
        'Methane', 'CH4')
    hot_conversion = _pick(
        (hot.get('results') or {}).get('reactant_conversion_percent') or {},
        'Methane', 'CH4')
    cold_h2 = component_flow(cold, 'Hydrogen', 'H2')
    hot_h2 = component_flow(hot, 'Hydrogen', 'H2')
    cold_ratio = ratio(cold, 'CO', 'CO2')
    hot_ratio = ratio(hot, 'CO', 'CO2')
    cold_duty = (cold.get('results') or {}).get('heat_duty_kW')
    hot_duty = (hot.get('results') or {}).get('heat_duty_kW')

    parts: list[str] = []
    conversion_change = None
    if isinstance(cold_conversion, (int, float)) and isinstance(
            hot_conversion, (int, float)):
        conversion_change = hot_conversion - cold_conversion
        parts.append('CH4 转化率 %s → %s（%+.2f 个百分点）'
                     % (_num(cold_conversion, 4), _num(hot_conversion, 4),
                        conversion_change))
    if isinstance(cold_h2, (int, float)) and isinstance(hot_h2, (int, float)):
        parts.append('H2 出口流量 %s → %s kmol/h'
                     % (_num(cold_h2, 2), _num(hot_h2, 2)))
    if cold_ratio is not None and hot_ratio is not None:
        parts.append('CO/CO2 摩尔比 %s → %s'
                     % (_num(cold_ratio, 3), _num(hot_ratio, 3)))
    if isinstance(cold_duty, (int, float)) and isinstance(hot_duty, (int, float)):
        parts.append('热负荷 %s → %s kW' % (_num(cold_duty, 2), _num(hot_duty, 2)))

    cold_temperature = _outlet_of(cold).get('temperature_C')
    hot_temperature = _outlet_of(hot).get('temperature_C')
    lines = ['温度趋势（%s → %s °C）：%s'
             % (_num(cold_temperature, 2), _num(hot_temperature, 2),
                '；'.join(parts)) + '。']

    components = set()
    for execution in (cold, hot):
        components |= set(_outlet_of(execution).get('mole_fractions') or {})
    if {'Methane', 'CO', 'CO2'} <= components:
        if conversion_change is not None and conversion_change > 0:
            lines.append('  重整反应吸热，升温使平衡向产物方向移动。')
        elif conversion_change is not None:
            lines.append('  与吸热重整的一般规律不一致，需要核查。')
        if cold_ratio is not None and hot_ratio is not None:
            if hot_ratio > cold_ratio:
                lines.append('  水煤气变换放热，升温抑制变换。')
            elif conversion_change is not None and conversion_change > 0:
                lines.append('  与吸热重整的一般规律不一致，需要核查。')
    return lines


# ------------------------------------------------------------------- adapters

def view_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """The report view of a graph state."""
    return {
        'status': state.get('status'),
        'decision': state.get('decision') or {},
        'blocking': state.get('blocking') or [],
        'open_questions': state.get('open_questions') or [],
        'assumptions': state.get('assumptions') or [],
        'executions': state.get('executions') or [],
        'problems': state.get('problems') or [],
    }


def view_from_run(run) -> dict[str, Any]:
    """The report view of a single-pass `AgentRun`, in the same shape."""
    decision = run.decision
    return {
        'status': run.status,
        'decision': {
            'preferred': decision.preferred_reactor if decision else None,
            'executed': decision.execution_reactor if decision else None,
            'capability': decision.capability_status if decision else None,
            'rule': decision.rule_id if decision else None,
            'explanation': decision.explanation if decision else '',
            'fallback_reason': decision.fallback_reason if decision else None,
            'substituted': decision.was_substituted() if decision else False,
        },
        'blocking': [{'id': q.id, 'field': q.field, 'question': q.question,
                      'default': q.default,
                      'reason': q.reason} for q in run.blocking_question_objects()],
        'open_questions': [q.question for q in (run.plan.questions if run.plan else [])
                           if not q.blocking],
        'assumptions': [{'id': a.id, 'field': a.field, 'value': a.value,
                         'scope': a.scope, 'source': a.source,
                         'accepted': a.accepted}
                        for a in (run.plan.assumptions if run.plan else [])],
        'executions': [results_view(e) for e in run.executions],
        'problems': list(run.problems),
    }


def results_view(execution: ExecutionResult) -> dict[str, Any]:
    """`summary()` plus `results()`, which is what both callers store."""
    return dict(**execution.summary(), results=execution.results())
