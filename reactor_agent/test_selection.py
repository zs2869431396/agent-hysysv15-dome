"""Selection-rule tests.

Every case here corresponds to a defect the plan found in the earlier draft
(`hysys-ai/phi_select.py`, plan 3.4) or to a scenario in the exam. The tests are
deliberately about *facts in, decision out*: no model is involved, so a failure
means the rules are wrong rather than that a model behaved oddly.
"""
from __future__ import annotations

import unittest
from unittest import mock

from reactor_agent import capabilities as caps
from reactor_agent.capabilities import combination_status, is_executable
from reactor_agent.schemas import (
    ConversionConstraint,
    KineticData,
    OperatingCaseRequest,
    ProcessRequest,
    ReactionSpec,
)
from reactor_agent.selection import (
    RULE_CONVERSION,
    RULE_GIBBS,
    RULE_INSUFFICIENT,
    RULE_KINETIC_CSTR,
    RULE_KINETIC_PFR,
    RULE_KINETIC_PHASE_UNCLEAR,
    RULE_KINETIC_POLYMER,
    RULE_YIELD_NOT_CONVERSION,
    select_reactor,
    selection_questions,
)

TOLUENE_REACTION = ReactionSpec(
    name='DISPROP', stoichiometry={'Toluene': -2.0, 'Benzene': 1.0, 'o-Xylene': 1.0})


def toluene_request(percent: float = 50.0) -> ProcessRequest:
    return ProcessRequest(
        source_text='请帮我完成甲苯歧化反应的模拟，甲苯进料流量10000kg/h，'
                    '进料温度为380℃，操作压力2.5MPa，甲苯转化率为%g%%' % percent,
        scenario_label='toluene',
        components=['Toluene', 'Benzene', 'o-Xylene'],
        reactions=[TOLUENE_REACTION],
        feeds=[],
        conversion_constraints=[ConversionConstraint(
            reaction='DISPROP', percent=percent, base_component='Toluene')],
        phase='liquid',
    )


class SelectionRules(unittest.TestCase):

    # ------------------------------------------------ exam scenario 2
    def test_toluene_selects_conversion(self):
        decision = select_reactor(toluene_request())
        self.assertEqual(decision.preferred_reactor, 'conversion')
        self.assertEqual(decision.rule_id, RULE_CONVERSION)
        self.assertTrue(decision.is_executable())

    def test_conversion_figure_is_the_evidence(self):
        """Defect 3: the choice must not be justified by a reactor type field.

        The request has no reactor-type field at all, so a Decision can only cite
        the stated conversion. This test pins that: the same request with a
        different percentage still selects Conversion for the same stated reason.
        """
        for percent in (50.0, 30.0, 12.5):
            with self.subTest(percent=percent):
                decision = select_reactor(toluene_request(percent))
                self.assertEqual(decision.rule_id, RULE_CONVERSION)
                self.assertIn('conversion figure', ' '.join(decision.evidence))

    def test_explanation_reports_the_actual_percentage(self):
        """Defect 4: the explanation hard-coded 50% and misreported 30%."""
        decision = select_reactor(toluene_request(30.0))
        self.assertIn('30', decision.explanation)
        self.assertNotIn('50', decision.explanation)

    # ------------------------------------------------ kinetics
    def _kinetic(self, phase: str, polymer: bool = False) -> ProcessRequest:
        return ProcessRequest(
            source_text='给出速率方程与反应器体积',
            kinetic_data=KineticData(rate_law='r = k*CA', pre_exponential=1e6,
                                     activation_energy_J_mol=50000.0,
                                     reactor_volume_m3=2.0),
            reactions=[ReactionSpec(stoichiometry={'A': -1.0, 'B': 1.0})],
            phase=phase, is_polymerisation=polymer)

    def test_liquid_kinetics_selects_cstr(self):
        """Defect 1: the old branch returned a type field, not CSTR."""
        decision = select_reactor(self._kinetic('liquid'))
        self.assertEqual(decision.preferred_reactor, 'cstr')
        self.assertEqual(decision.rule_id, RULE_KINETIC_CSTR)

    def test_gas_kinetics_selects_pfr(self):
        decision = select_reactor(self._kinetic('gas'))
        self.assertEqual(decision.preferred_reactor, 'pfr')
        self.assertEqual(decision.rule_id, RULE_KINETIC_PFR)

    def test_pfr_selection_is_never_rewritten(self):
        """Plan 5.4: an unsupported PFR stays a PFR, reported as unsupported."""
        decision = select_reactor(self._kinetic('gas'))
        self.assertEqual(decision.preferred_reactor, 'pfr')
        self.assertIsNone(decision.execution_reactor)
        self.assertEqual(decision.capability_status, 'unsupported')
        self.assertFalse(decision.is_executable())

    def test_mixed_phase_asks_instead_of_assuming(self):
        """Defect 2: anything not exactly 'liquid' used to become vapour."""
        for phase in ('mixed', 'unknown'):
            with self.subTest(phase=phase):
                decision = select_reactor(self._kinetic(phase))
                self.assertEqual(decision.rule_id, RULE_KINETIC_PHASE_UNCLEAR)
                self.assertIsNone(decision.execution_reactor)
                self.assertNotEqual(decision.preferred_reactor, 'pfr')

    def test_polymerisation_does_not_use_the_phase_rule(self):
        """The exam excludes polymerisation from the liquid-CSTR rule."""
        decision = select_reactor(self._kinetic('liquid', polymer=True))
        self.assertEqual(decision.rule_id, RULE_KINETIC_POLYMER)
        self.assertNotEqual(decision.preferred_reactor, 'cstr')

    def test_equipment_only_is_not_kinetic_data(self):
        """Plan 5.4: "管式/釜式" alone must not count as kinetic data."""
        request = ProcessRequest(
            source_text='这是一个管式反应器',
            kinetic_data=KineticData(rate_law='', reactor_volume_m3=1.0),
            reactions=[ReactionSpec(stoichiometry={'A': -1.0, 'B': 1.0})])
        self.assertFalse(request.has_kinetics())
        decision = select_reactor(request)
        self.assertNotEqual(decision.rule_id, RULE_KINETIC_PFR)
        self.assertEqual(decision.rule_id, RULE_INSUFFICIENT)

    # ------------------------------------------------ yields
    def test_yield_alone_does_not_select_conversion(self):
        """Defect 6: yield/selectivity is not a conversion."""
        request = ProcessRequest(
            source_text='要求 CO 收率 85%，选择性 90%',
            reactions=[ReactionSpec(stoichiometry={'C': -1.0, 'CO': 1.0})])
        decision = select_reactor(request)
        self.assertEqual(decision.rule_id, RULE_YIELD_NOT_CONVERSION)
        self.assertIsNone(decision.execution_reactor)

    # ------------------------------------------------ gibbs
    def test_high_temperature_many_species_selects_gibbs(self):
        request = ProcessRequest(
            source_text='高温气化，出口 1400 度',
            components=['Carbon', 'Water', 'CO', 'Hydrogen', 'CO2', 'Methane'],
            reactions=[ReactionSpec(stoichiometry={'Carbon': -1.0, 'Water': -1.0,
                                                   'CO': 1.0, 'Hydrogen': 1.0})],
            operating_cases=[OperatingCaseRequest(
                case_id='gasifier', outlet_temperature=1400.0,
                outlet_temperature_unit='C', thermal_mode='isothermal')])
        decision = select_reactor(request)
        self.assertEqual(decision.preferred_reactor, 'gibbs')
        self.assertEqual(decision.rule_id, RULE_GIBBS)

    def test_missing_information_does_not_default_to_gibbs(self):
        """Defect 5: "no reactions" used to fall through to Gibbs."""
        request = ProcessRequest(source_text='帮我模拟一个反应',
                                 reactions=[])
        decision = select_reactor(request)
        self.assertEqual(decision.rule_id, RULE_INSUFFICIENT)
        self.assertIsNone(decision.execution_reactor)

    def test_low_temperature_without_kinetics_is_not_gibbs(self):
        request = ProcessRequest(
            source_text='常温反应',
            reactions=[ReactionSpec(stoichiometry={'A': -1.0, 'B': 1.0})],
            operating_cases=[OperatingCaseRequest(
                case_id='c1', outlet_temperature=60.0, thermal_mode='isothermal')])
        decision = select_reactor(request)
        self.assertNotEqual(decision.rule_id, RULE_GIBBS)


class Questions(unittest.TestCase):

    def test_selection_questions_are_blocking(self):
        decision = select_reactor(ProcessRequest(source_text='一个反应', reactions=[]))
        questions = selection_questions(decision, ProcessRequest(
            source_text='一个反应', reactions=[]))
        self.assertTrue(questions)
        self.assertTrue(all(q.blocking for q in questions))
        self.assertTrue(all(q.is_open() for q in questions))


class CapabilityLookup(unittest.TestCase):

    def test_verified_combinations(self):
        self.assertEqual(combination_status('conversion', 'adiabatic',
                                            reaction_count=1)['status'], 'verified')
        self.assertEqual(combination_status('gibbs', 'isothermal',
                                            reaction_count=1)['status'], 'verified')

    def test_unsupported_combinations_are_not_silently_allowed(self):
        for kind, thermal, count, solid in (
                ('gibbs', 'adiabatic', 1, False),
                ('equilibrium', 'adiabatic', 1, False),
                ('equilibrium', 'isothermal', 1, True),
                ('conversion', 'adiabatic', 2, False),
                ('cstr', 'isothermal', 1, False),
                ('pfr', 'isothermal', 1, False)):
            with self.subTest(kind=kind, thermal=thermal, count=count):
                self.assertEqual(
                    combination_status(kind, thermal, reaction_count=count,
                                       solid_phase=solid)['status'], 'unsupported')
                self.assertFalse(is_executable(kind, thermal, reaction_count=count,
                                               solid_phase=solid))

    def test_solid_carbon_gibbs_is_verified_via_saturation(self):
        status = combination_status('gibbs', 'isothermal', reaction_count=1,
                                    solid_phase=True)
        self.assertEqual(status['status'], 'verified')
        self.assertEqual(status['rule'], 'gibbs_isothermal_solid_saturation')
        self.assertTrue(status['evidence'])

    def test_every_status_cites_evidence_or_explains_absence(self):
        for kind, thermal in (('conversion', 'adiabatic'), ('gibbs', 'isothermal'),
                              ('gibbs', 'adiabatic'), ('equilibrium', None),
                              ('cstr', None)):
            with self.subTest(kind=kind, thermal=thermal):
                status = combination_status(kind, thermal)
                self.assertTrue(status['reason'])
                if status['status'] == 'verified':
                    self.assertTrue(status['evidence'],
                                    'a verified combination must cite a real run')

    # ------------------------------------------- the accepted combination table
    def test_the_accepted_table_is_reproduced_row_by_row(self):
        """Plan 2.3: one row per combination, no row implied by another."""
        for kind, thermal, phase, count, solid, expected, rule in (
                ('cstr', None, None, 1, False, 'unsupported', 'no_implementation'),
                ('pfr', None, None, 1, False, 'unsupported', 'no_implementation'),
                ('conversion', 'adiabatic', None, 2, False, 'unsupported',
                 'multiple_conversion_reactions'),
                ('conversion', 'adiabatic', None, 1, False, 'verified',
                 'conversion_adiabatic_single'),
                ('conversion', 'isothermal', None, 1, False, 'experimental',
                 'conversion_isothermal_single'),
                ('conversion', None, None, 1, False, 'experimental',
                 'conversion_unspecified_thermal'),
                ('equilibrium', 'isothermal', None, 1, True, 'unsupported',
                 'equilibrium_solid'),
                ('equilibrium', 'isothermal', 'liquid', 1, False, 'unsupported',
                 'equilibrium_liquid'),
                ('equilibrium', None, None, 1, False, 'unsupported',
                 'equilibrium_needs_isothermal'),
                ('equilibrium', 'adiabatic', None, 1, False, 'unsupported',
                 'equilibrium_needs_isothermal'),
                ('equilibrium', 'isothermal', 'gas', 1, False, 'verified',
                 'equilibrium_isothermal_vapour'),
                ('gibbs', 'adiabatic', None, 1, False, 'unsupported',
                 'gibbs_adiabatic_unimplemented'),
                ('gibbs', None, None, 1, False, 'experimental',
                 'gibbs_unspecified_thermal'),
                ('gibbs', 'isothermal', None, 1, True, 'verified',
                 'gibbs_isothermal_solid_saturation'),
                ('gibbs', 'isothermal', None, 1, False, 'verified',
                 'gibbs_isothermal_gas')):
            with self.subTest(kind=kind, thermal=thermal, phase=phase,
                              count=count, solid=solid):
                status = combination_status(kind, thermal, phase=phase,
                                            reaction_count=count, solid_phase=solid)
                self.assertEqual(status['status'], expected)
                self.assertEqual(status['rule'], rule)

    def test_is_executable_agrees_with_the_status(self):
        """Plan 2.5: the boolean is a view of the table, not a second table."""
        for kind, thermal, phase, count, solid in (
                ('conversion', 'adiabatic', None, 1, False),
                ('equilibrium', 'isothermal', 'gas', 1, False),
                ('equilibrium', 'adiabatic', None, 1, False),
                ('gibbs', 'isothermal', None, 1, True),
                ('gibbs', 'adiabatic', None, 1, False),
                ('cstr', 'isothermal', None, 1, False)):
            with self.subTest(kind=kind, thermal=thermal):
                expected = combination_status(
                    kind, thermal, phase=phase, reaction_count=count,
                    solid_phase=solid)['status'] != 'unsupported'
                self.assertEqual(
                    is_executable(kind, thermal, phase=phase,
                                  reaction_count=count, solid_phase=solid), expected)

    def test_a_changed_tool_layer_downgrades_verified(self):
        """Plan 11: verified rests on bytes, so changing one byte revokes it."""
        caps.acceptance_state.cache_clear()
        try:
            with mock.patch.object(caps, '_file_sha256', return_value='0' * 64):
                caps.acceptance_state.cache_clear()
                status = caps.combination_status('equilibrium', 'isothermal')
        finally:
            caps.acceptance_state.cache_clear()
        self.assertEqual(status['status'], 'experimental')
        self.assertIn('不一致', status['reason'])
        self.assertEqual(status['evidence'], [])

    def test_acceptance_state_reads_as_holding_for_this_tool_layer(self):
        state = caps.acceptance_state()
        self.assertTrue(state['holds'], state['reason'])
        self.assertEqual(state['tool_revision'], caps.ACCEPTED_TOOL_REVISION)
        self.assertEqual(state['mismatched'], [])

    def test_no_combination_outside_the_record_is_verified(self):
        """Plan 11 / review brief 5: catch a row that quietly claims verification."""
        accepted_rules = {
            'conversion_adiabatic_single',
            'equilibrium_isothermal_vapour',
            'gibbs_isothermal_gas',
            'gibbs_isothermal_solid_saturation',
        }
        for kind in ('conversion', 'equilibrium', 'gibbs', 'cstr', 'pfr'):
            for thermal in ('adiabatic', 'isothermal', None):
                for solid in (False, True):
                    with self.subTest(kind=kind, thermal=thermal, solid=solid):
                        status = combination_status(kind, thermal, phase='gas',
                                                    solid_phase=solid)
                        self.assertIn(status['status'],
                                      ('verified', 'experimental', 'unsupported'))
                        if status['status'] == 'verified':
                            self.assertIn(status['rule'], accepted_rules)

    def test_equilibrium_acceptance_cites_reforming_not_the_gasifier(self):
        """The two verified Gibbs-family rows must stay distinguishable."""
        vapour = combination_status('equilibrium', 'isothermal')
        solid = combination_status('gibbs', 'isothermal', solid_phase=True)
        self.assertNotEqual(vapour['rule'], solid['rule'])
        self.assertIn('重整', vapour['reason'])
        self.assertIn('LIQUID', solid['reason'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
