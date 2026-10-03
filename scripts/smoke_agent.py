"""End-to-end smoke test of the agent front half, short of running HYSYS.

    natural language -> extract -> ground -> normalise -> select -> compile -> precheck

It stops at the pre-check because this machine has no HYSYS. Everything before that
is real: a real model call, real normalisation, the real rule layer, the real
compiler and the real pre-check. What it proves is that a sentence can become a spec
the tool layer would accept, with no hand-written JSON in between.

Usage (credentials come from the environment):

    python scripts/smoke_agent.py                 # toluene, the default
    python scripts/smoke_agent.py smr gasification
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hysys_tools import precheck                          # noqa: E402
from reactor_agent.capabilities import combination_status  # noqa: E402
from reactor_agent.compiler import compile_plan            # noqa: E402
from reactor_agent.extraction import EXTRACTION_SCHEMA, extract_verified  # noqa: E402
from reactor_agent.llm import ChatClient, LlmConfig, LlmError, describe_config  # noqa: E402
from reactor_agent.normalize import normalize              # noqa: E402
from reactor_agent.schemas import ModelingPlan, OperatingCase  # noqa: E402
from reactor_agent.selection import select_reactor         # noqa: E402

# The exam's own wording, verbatim.
SCENARIOS = {
    'toluene': dict(
        kind='conversion', phase='liquid', feed_basis='mass_fraction',
        label='toluene',
        text='请帮我完成甲苯歧化反应的模拟，甲苯原料进入转化率反应器，发生歧化反应：'
             '2C₇H₈ → C₆H₆ + C₈H₁₀。甲苯进料流量10000kg/h，进料温度为380℃，'
             '操作压力2.5MPa，甲苯转化率为50%，反应产物为苯和二甲苯（邻、间、对三种异构体），'
             '请配置反应并模拟产物分布和流股组成'),
    'smr': dict(
        kind='gibbs', phase='gas', feed_basis='molar_fraction', label='smr',
        text='我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7）有两个反应，'
             '主反应甲烷和水反应生成一氧化碳和氢气；副反应一氧化碳和水蒸汽反应生成'
             '二氧化碳和氢气，请分析以下两种情况下反应炉的组分分布：'
             '1、重整炉出口气温度为 710°C，压力 13.5 bar，进料温度520℃；'
             '2、重整炉出口气温度为600℃，压力13.5bar，进料温度520℃。'
             '进料流量可以自定，要求符合一个工厂一年正常的处理量'),
    'gasification': dict(
        kind='gibbs', phase='gas', feed_basis='mass_fraction', label='gasification',
        text='我要模拟水煤浆的气化过程。进料为煤炭和水，流量80000Nm3/h，压力40bar，'
             '水煤浆进料浓度62wt%，进料温度40摄氏度，主要反应：C+H2O → CO+H2。'
             '请帮我计算一下气化炉出口温度为1400度时出口组成及CO的收率，反应器灰分不做考虑'),
}

# Fields the exam explicitly leaves to the user, so a value there is chosen rather
# than quoted. Grounding must not flag them.
ALLOWED_BY_SCENARIO = {'smr': {'feed_total'}}


def run(name: str, sink: dict) -> None:
    scenario = SCENARIOS[name]
    print('\n' + '=' * 72)
    print('scenario: %s' % name)
    print('=' * 72)

    config = LlmConfig.from_env()
    print('config: %s' % json.dumps(describe_config(config), ensure_ascii=False))
    client = ChatClient(config, logger=lambda m: print('  [llm] %s' % m))
    print('system prompt fields: %d' % len(EXTRACTION_SCHEMA['properties']))

    # ---------------------------------------------------------------- ② extract
    extraction, problems = extract_verified(
        client, scenario['text'], scenario['kind'],
        allowed=ALLOWED_BY_SCENARIO.get(name, set()))
    print('attempts=%d  error=%s' % (extraction.attempts, extraction.error))
    print('facts: %s' % json.dumps(extraction.facts, ensure_ascii=False)[:400])
    if extraction.missing_information:
        print('model says unstated: %s'
              % json.dumps(extraction.missing_information, ensure_ascii=False)[:300])
    if problems:
        print('PROBLEMS:')
        for item in problems:
            print('  - %s' % item)

    # -------------------------------------------------------------- ③ validate
    if extraction.error:
        sink[name] = {'stage': 'extract', 'problems': problems}
        return

    request, report = normalize(extraction.facts, scenario['text'],
                                scenario_label=scenario['label'],
                                phase=scenario['phase'],
                                feed_basis=scenario['feed_basis'])
    print('components: %s' % request.components)
    print('reactions : %s' % [r.stoichiometry for r in request.reactions])
    print('cases     : %s' % [c.outlet_temperature for c in request.operating_cases])
    if report.applied:
        print('normalised:')
        for item in report.applied:
            print('  ~ %s' % item)
    blocking = report.blocking
    if blocking:
        print('BLOCKING QUESTIONS (nothing would be executed):')
        for question in blocking:
            print('  ? %s' % question.question)

    # ---------------------------------------------------------------- ④ select
    decision = select_reactor(request)
    print('selection : preferred=%s execution=%s status=%s rule=%s'
          % (decision.preferred_reactor, decision.execution_reactor,
             decision.capability_status, decision.rule_id))
    print('reason    : %s' % decision.explanation[:160])

    # --------------------------------------------------------------- ⑤ compile
    plan = ModelingPlan(request=request, decision=decision,
                        components=request.components,
                        thermal_mode='isothermal' if request.operating_cases
                        else 'adiabatic',
                        cases=[OperatingCase(case_id=c.case_id, label=c.label)
                               for c in request.operating_cases]
                        or [OperatingCase(case_id='single')])
    plan.questions.extend(report.questions)
    plan.assumptions.extend(report.assumptions)
    compiled = compile_plan(plan)
    print('plan status: %s' % compiled.status)
    if report.agent_choices:
        print('ASSUMPTIONS WE MADE (must appear in the report):')
        for item in report.agent_choices:
            print('  * %s = %s  (%s)' % (item.field, item.value, item.scope))

    if compiled.status != 'READY':
        print('not executable: %s' % ', '.join(
            q.question for q in compiled.blocking_questions())[:300])
        sink[name] = {'stage': 'compile', 'status': compiled.status,
                      'blocking': [q.question for q in compiled.blocking_questions()]}
        return

    for case in compiled.cases:
        spec = case.spec
        check = precheck.validate_spec(spec)
        print('case %-10s precheck=%s %s'
              % (case.case_id, 'OK' if check['ok'] else 'FAILED',
                 '' if check['ok'] else check['errors'][:2]))
        sink[name] = {'stage': 'ready', 'case_id': case.case_id,
                      'precheck_ok': check['ok'], 'spec': spec,
                      'blocking_questions': [q.question for q in plan.blocking_questions()]}


def main(argv: list[str]) -> int:
    names = argv or ['toluene']
    sink: dict = {}
    for name in names:
        if name not in SCENARIOS:
            print('unknown scenario %r; choose from %s' % (name, sorted(SCENARIOS)))
            return 2
        try:
            run(name, sink)
        except LlmError as exc:
            print('model error: %s' % exc)
            sink[name] = {'stage': 'llm_error', 'error': str(exc)}
        except Exception as exc:                        # noqa: BLE001
            import traceback
            traceback.print_exc()
            sink[name] = {'stage': 'exception', 'error': '%s: %s'
                          % (type(exc).__name__, exc)}

    out = ROOT / '_demo' / 'smoke-agent.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sink, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\nwritten: %s' % out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
