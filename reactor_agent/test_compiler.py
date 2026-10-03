"""Compiler tests: ModelingPlan -> hysys-agent/spec/1.

The toluene and reformer cases are compared against the parameters that were
actually run on the workstation (`hysys_tools/examples.py`, verified by
`tool-layer-runs/acceptance-20261002-144131-d442afae`). A compiler change that
silently alters a verified input therefore fails here instead of costing a remote
run, and every compiled spec is put through the tool layer's own pre-check.
"""
from __future__ import annotations

import unittest

import pydantic
from hysys_tools import examples, precheck

from reactor_agent.capabilities import combination_status
from reactor_agent.compiler import (
    CompileError,
    coal_questions,
    compile_plan,
    feed_questions,
    safe_case_name,
)
from reactor_agent.schemas import (
    ConversionConstraint,
    FeedSpec,
    ModelingPlan,
    OperatingCase,
    OperatingCaseRequest,
    ProcessRequest,
    ReactionSpec,
)
from reactor_agent.selection import select_reactor

# Exactly the stoichiometry that was verified on the workstation. The three xylene
# isomers each take one third, which is why the verified outlet shows three equal
# xylene flows.
TOLUENE_STOICH = {'Toluene': -2.0, 'Benzene': 1.0,
                  'o-Xylene': 1 / 3, 'm-Xylene': 1 / 3, 'p-Xylene': 1 / 3}
TOLUENE_COMPONENTS = ['Toluene', 'Benzene', 'o-Xylene', 'm-Xylene', 'p-Xylene']
SMR_COMPONENTS = ['Methane', 'Water', 'CO', 'Hydrogen', 'CO2']


def toluene_plan(percent: float = 50.0) -> ModelingPlan:
    request = ProcessRequest(
        source_text='甲苯进料流量10000kg/h，进料温度380℃，操作压力2.5MPa，'
                    '甲苯转化率为%g%%' % percent,
        scenario_label='toluene',
        components=TOLUENE_COMPONENTS,
        reactions=[ReactionSpec(name='DISPROP', stoichiometry=TOLUENE_STOICH)],
        feeds=[FeedSpec(basis='mass_fraction', fractions={'Toluene': 1.0},
                        total_flow=10000.0, total_flow_unit='kg/h',
                        temperature=380.0, temperature_unit='C',
                        pressure=2500.0, pressure_unit='kPa')],
        conversion_constraints=[ConversionConstraint(
            reaction='DISPROP', percent=percent, base_component='Toluene')],
        phase='liquid',
        operating_cases=[OperatingCaseRequest(case_id='tol', thermal_mode='adiabatic')])
    decision = select_reactor(request)
    return ModelingPlan(request=request, decision=decision,
                        components=list(TOLUENE_COMPONENTS),
                        thermal_mode='adiabatic',
                        cases=[OperatingCase(case_id='tol', label='toluene')])


def smr_plan(temperatures=(710.0, 600.0)) -> ModelingPlan:
    # The exam states two reactions: reforming itself and the water-gas shift.
    # They are recorded here because they are what the user said, even though a
    # Gibbs specification carries no reaction equations - that separation between
    # "what was asked" and "what is executed" is the point of the three layers.
    request = ProcessRequest(
        source_text='甲烷蒸汽重整，进料甲烷和水蒸气摩尔比 1:2.7，进料温度520℃，'
                    '压力13.5bar，出口温度分别为710℃和600℃。'
                    '主反应甲烷和水反应生成一氧化碳和氢气；'
                    '副反应一氧化碳和水蒸汽反应生成二氧化碳和氢气',
        scenario_label='smr',
        components=SMR_COMPONENTS,
        reactions=[
            ReactionSpec(name='SMR', reversible=True, stoichiometry={
                'Methane': -1.0, 'Water': -1.0, 'CO': 1.0, 'Hydrogen': 3.0}),
            ReactionSpec(name='WGS', reversible=True, stoichiometry={
                'CO': -1.0, 'Water': -1.0, 'CO2': 1.0, 'Hydrogen': 1.0}),
        ],
        feeds=[FeedSpec(basis='molar_flow',
                        flows={'Methane': 1000.0, 'Water': 2700.0},
                        total_flow_unit='kmol/h',
                        temperature=520.0, temperature_unit='C',
                        pressure=13.5, pressure_unit='bar')],
        operating_cases=[OperatingCaseRequest(
            case_id='smr-%g' % t, label='%gC' % t, outlet_temperature=t,
            outlet_temperature_unit='C', thermal_mode='isothermal')
            for t in temperatures],
        phase='gas')
    decision = select_reactor(request)
    return ModelingPlan(
        request=request, decision=decision, components=list(SMR_COMPONENTS),
        thermal_mode='isothermal',
        cases=[OperatingCase(case_id='smr-%g' % t, label='%gC' % t,
                             overrides={'outlet_temperature': t,
                                        'outlet_temperature_unit': 'C'})
               for t in temperatures])


class TolueneCompilation(unittest.TestCase):

    def test_compiles_and_passes_the_precheck(self):
        plan = compile_plan(toluene_plan())
        self.assertEqual(plan.status, 'READY')
        spec = plan.cases[0].spec
        self.assertIsNotNone(spec)
        report = precheck.validate_spec(spec)
        self.assertTrue(report['ok'], report['errors'])

    def test_verified_inputs_survive_compilation(self):
        """Every figure that was actually run must come through unchanged."""
        spec = compile_plan(toluene_plan()).cases[0].spec
        reference = examples.toluene_spec()
        self.assertEqual(spec['feeds'][0]['total_flow'],
                         reference['feeds'][0]['total_flow'])
        self.assertEqual(spec['feeds'][0]['temperature'],
                         reference['feeds'][0]['temperature'])
        self.assertEqual(spec['reactor']['thermal_mode'],
                         reference['reactor']['thermal_mode'])
        self.assertEqual(spec['reactions'][0]['conversion_percent'],
                         reference['reactions'][0]['conversion_percent'])
        self.assertEqual(spec['reactions'][0]['base_component'],
                         reference['reactions'][0]['base_component'])

    def test_stoichiometry_is_carried_through(self):
        spec = compile_plan(toluene_plan()).cases[0].spec
        stoich = spec['reactions'][0]['stoichiometry']
        for name, coefficient in TOLUENE_STOICH.items():
            self.assertAlmostEqual(stoich[name], coefficient, places=12)

    def test_a_different_conversion_reaches_the_spec(self):
        """The run must not return a fixed example: 30% has to mean 30%."""
        spec = compile_plan(toluene_plan(30.0)).cases[0].spec
        self.assertEqual(spec['reactions'][0]['conversion_percent'], 30.0)
        self.assertNotEqual(spec['reactions'][0]['conversion_percent'], 50.0)
        self.assertTrue(precheck.validate_spec(spec)['ok'])

    def test_pressure_unit_is_normalised_to_something_the_tool_accepts(self):
        spec = compile_plan(toluene_plan()).cases[0].spec
        report = precheck.validate_spec(spec)
        self.assertEqual(report['readback']['feed_kPa'], 2500.0)

    def test_case_name_is_windows_safe(self):
        plan = toluene_plan()
        plan.cases[0].overrides['case_name'] = '../../etc/passwd'
        spec = compile_plan(plan).cases[0].spec
        self.assertNotIn('/', spec['case_name'])
        self.assertNotIn('..', spec['case_name'])


class ReformerCompilation(unittest.TestCase):

    def test_two_cases_get_two_specs_with_different_temperatures(self):
        plan = compile_plan(smr_plan())
        self.assertEqual(plan.status, 'READY')
        self.assertEqual(len(plan.cases), 2)
        temperatures = [c.spec['reactor']['outlet_temperature'] for c in plan.cases]
        self.assertEqual(temperatures, [710.0, 600.0])
        self.assertNotEqual(plan.cases[0].spec_hash, plan.cases[1].spec_hash)

    def test_feed_temperature_is_not_confused_with_the_outlet(self):
        """The plan warns that 520 C inlet must not become the outlet setpoint."""
        plan = compile_plan(smr_plan())
        for case in plan.cases:
            self.assertEqual(case.spec['feeds'][0]['temperature'], 520.0)
            self.assertNotEqual(case.spec['reactor']['outlet_temperature'], 520.0)

    def test_each_spec_passes_the_precheck(self):
        for case in compile_plan(smr_plan()).cases:
            with self.subTest(case=case.case_id):
                report = precheck.validate_spec(case.spec)
                self.assertTrue(report['ok'], report['errors'])

    def test_gibbs_spec_carries_no_reactions(self):
        """A Gibbs reactor distributes by free energy; equations would be ignored."""
        for case in compile_plan(smr_plan()).cases:
            self.assertEqual(case.spec['reactions'], [])

    def test_spec_hash_changes_when_the_case_changes(self):
        plan = smr_plan()
        before = compile_plan(plan).cases[0].spec_hash
        plan.cases[0].overrides['outlet_temperature'] = 700.0
        after = compile_plan(plan).cases[0].spec_hash
        self.assertNotEqual(before, after)


class GasificationIsBlockedNotGuessed(unittest.TestCase):
    """The exam gives 80000 Nm3/h for a solid-plus-liquid feed. That cannot be
    converted without knowing the stream and the standard conditions, and picking
    an interpretation would yield a confident but unverifiable CO yield."""

    def _plan(self) -> ModelingPlan:
        request = ProcessRequest(
            source_text='我要模拟水煤浆的气化过程。进料为煤炭和水，'
                        '流量80000Nm3/h，压力40bar，水煤浆进料浓度62wt%，'
                        '进料温度40摄氏度，主要反应：C+H2O → CO+H2',
            scenario_label='gasification',
            components=['Carbon', 'Water', 'CO', 'Hydrogen', 'CO2', 'Methane'],
            reactions=[ReactionSpec(stoichiometry={'Carbon': -1.0, 'Water': -1.0,
                                                   'CO': 1.0, 'Hydrogen': 1.0})],
            feeds=[FeedSpec(basis='mass_fraction',
                            fractions={'Carbon': 0.62, 'Water': 0.38},
                            total_flow=80000.0, total_flow_unit='Nm3/h',
                            temperature=40.0, pressure=40.0, pressure_unit='bar')],
            operating_cases=[OperatingCaseRequest(
                case_id='gasifier', outlet_temperature=1400.0,
                outlet_temperature_unit='C', thermal_mode='isothermal')])
        decision = select_reactor(request)
        return ModelingPlan(request=request, decision=decision,
                            components=list(request.components),
                            thermal_mode='isothermal',
                            cases=[OperatingCase(case_id='gasifier')])

    def test_selection_still_recognises_a_gibbs_system(self):
        """Blocked on input, not on model choice: the reactor must still be Gibbs."""
        plan = self._plan()
        self.assertEqual(plan.decision.preferred_reactor, 'gibbs')

    def test_the_volume_flow_raises_a_blocking_question(self):
        plan = self._plan()
        questions = feed_questions(plan.request)
        self.assertTrue(questions)
        self.assertTrue(all(q.blocking for q in questions))
        self.assertTrue(any('80000' in q.question for q in questions))

    def test_coal_definition_is_questioned(self):
        plan = self._plan()
        questions = coal_questions(plan.request)
        self.assertTrue(questions)
        self.assertTrue(any('纯' in q.question or '碳' in q.question
                            for q in questions))

    def test_compilation_stops_without_producing_a_spec(self):
        plan = compile_plan(self._plan())
        self.assertEqual(plan.status, 'WAITING_INPUT')
        self.assertIsNone(plan.cases[0].spec)
        self.assertTrue(plan.blocking_questions())
        self.assertFalse(plan.is_ready())

    def test_the_unit_is_never_silently_converted(self):
        """Whatever else happens, Nm3/h must not become kg/h behind the user's back."""
        plan = compile_plan(self._plan())
        self.assertIsNone(plan.cases[0].spec)
        self.assertNotIn('kg/h', ' '.join(
            q.question for q in plan.blocking_questions()))


class AnsweringTheQuestionsUnblocksTheRun(unittest.TestCase):

    def test_answering_lets_the_plan_compile(self):
        """Once the basis is stated as a mass flow, the same request compiles.

        This is the demonstration that the guard is about missing information
        rather than about an inability to model gasification.
        """
        request = ProcessRequest(
            source_text='水煤浆气化，进料 80000 kg/h，62wt% 煤，40 度，40 bar，'
                        '出口 1400 度，煤按纯碳处理',
            scenario_label='gasification',
            components=['Carbon', 'Water', 'CO', 'Hydrogen', 'CO2', 'Methane'],
            reactions=[],
            feeds=[FeedSpec(basis='mass_fraction',
                            fractions={'Carbon': 0.62, 'Water': 0.38},
                            total_flow=80000.0, total_flow_unit='kg/h',
                            temperature=40.0, pressure=40.0, pressure_unit='bar')],
            operating_cases=[OperatingCaseRequest(
                case_id='gasifier', outlet_temperature=1400.0,
                outlet_temperature_unit='C', thermal_mode='isothermal')],
            phase='gas')
        decision = select_reactor(request)
        plan = ModelingPlan(request=request, decision=decision,
                            components=list(request.components),
                            thermal_mode='isothermal',
                            cases=[OperatingCase(case_id='gasifier')])
        compiled = compile_plan(plan)
        self.assertEqual(compiled.status, 'READY', compiled.blocking_questions())
        report = precheck.validate_spec(compiled.cases[0].spec)
        self.assertTrue(report['ok'], report['errors'])
        flows = report['readback']['feed_molar_flows_kmol_h']
        # 62 wt% carbon of 80000 kg/h, over 12.011 kg/kmol.
        self.assertAlmostEqual(flows['Carbon'], 0.62 * 80000.0 / 12.011, places=3)


class ContractGuards(unittest.TestCase):

    def test_basis_and_unit_must_agree(self):
        """A mass basis with the default molar unit is caught at the contract."""
        with self.assertRaises(pydantic.ValidationError):
            FeedSpec(basis='mass_flow', flows={'A': 1.0}, total_flow_unit='kmol/h')

    def test_the_stack_of_units_the_tool_accepts_is_accepted_here(self):
        for unit in ('kg/h', 't/h', 'kgh'):
            with self.subTest(unit=unit):
                FeedSpec(basis='mass_flow', flows={'A': 1.0}, total_flow_unit=unit)

    def test_conversion_must_be_a_percentage(self):
        """Out-of-range figures are rejected at the contract.

        Note that 0.5 is a *legal* 0.5%, and the contract cannot distinguish it from
        someone who meant 50% and typed the fraction. That ambiguity has to be caught
        during intake, where the surrounding sentence is visible - it is why the
        tool layer's message says "write 50 for 50%, not 0.5".
        """
        for bad in (0, -5, 150, 100.0001):
            with self.subTest(percent=bad):
                with self.assertRaises(pydantic.ValidationError):
                    ConversionConstraint(percent=bad, base_component='Toluene')
        # The bound itself must be accepted.
        ConversionConstraint(percent=100, base_component='Toluene')
        ConversionConstraint(percent=0.5, base_component='Toluene')  # legal 0.5%

    def test_unknown_fields_are_rejected(self):
        """An LLM must not smuggle in a field we silently ignore."""
        with self.assertRaises(pydantic.ValidationError):
            ProcessRequest(source_text='x', unknown_field=1)

    def test_safe_case_name_handles_hostile_input(self):
        self.assertEqual(safe_case_name('../../etc/passwd'),
                         'etc-passwd')
        self.assertEqual(safe_case_name('CON'), 'agent-case')
        self.assertEqual(safe_case_name(''), 'agent-case')
        self.assertEqual(safe_case_name(None), 'agent-case')
        self.assertLessEqual(len(safe_case_name('x' * 200)), 60)

    def test_unsupported_plan_is_not_compiled_into_a_spec(self):
        """PFR is the right answer for a gas-phase kinetic system, and stays PFR."""
        request = ProcessRequest(
            source_text='气相动力学反应',
            components=['Methane'],
            kinetic_data={'rate_law': 'r = k*CA', 'pre_exponential': 1e6,
                          'activation_energy_J_mol': 50000.0,
                          'reactor_volume_m3': 1.0},
            reactions=[ReactionSpec(stoichiometry={'Methane': -1.0, 'CO': 1.0})],
            phase='gas')
        decision = select_reactor(request)
        self.assertEqual(decision.preferred_reactor, 'pfr')
        plan = ModelingPlan(request=request, decision=decision,
                            components=['Methane'], thermal_mode='isothermal',
                            cases=[OperatingCase(case_id='pfr-case')])
        compiled = compile_plan(plan)
        self.assertEqual(compiled.status, 'UNSUPPORTED')
        self.assertIsNone(compiled.cases[0].spec)


if __name__ == '__main__':
    unittest.main(verbosity=2)
