"""Tests for deterministic normalisation.

The properties that matter here are the negative ones: what normalisation refuses to
do. It must not invent a missing value, and it must not convert a unit that cannot be
converted - because a converted Nm3/h would look like a successful extraction while
actually encoding an unstated assumption.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from reactor_agent.normalize import (
    SPECIES_ALIASES,
    _stable_id,
    flow_is_ours_to_choose,
    normalize,
    resolve_species,
)

TOLUENE_TEXT = '甲苯进料流量10000kg/h，进料温度为380℃，操作压力2.5MPa，甲苯转化率为50%'

TOLUENE_FACTS = {
    'species': ['甲苯', '苯', '邻二甲苯', '间二甲苯', '对二甲苯'],
    'feed_composition': [{'name': '甲苯', 'fraction': 1.0}],
    'composition_basis': 'pure',
    'reactions': [{'name': '歧化',
                   'species': [{'name': '甲苯', 'coefficient': -2},
                   {'name': '苯', 'coefficient': 1},
                                {'name': '邻二甲苯', 'coefficient': 0.3333333333333333},
                                {'name': '间二甲苯', 'coefficient': 0.3333333333333333},
                                {'name': '对二甲苯', 'coefficient': 0.3333333333333333}],
                   'reversible': False}],
    'conversion_percent': 50, 'conversion_basis': '甲苯',
    'feed_total': 10000, 'feed_unit': 'kg/h',
    'feed_temperature': 380, 'feed_temperature_unit': '℃',
    'feed_pressure': 2.5, 'feed_pressure_unit': 'MPa',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [], 'outlet_temperature_unit': '',
    'missing_information': [],
}

SMR_FACTS = {
    'species': ['甲烷', '水', '一氧化碳', '氢气', '二氧化碳'],
    'feed_composition': [{'name': '甲烷', 'fraction': 1.0},
                         {'name': '水', 'fraction': 2.7}],
    'composition_basis': 'mole_ratio',
    'reactions': [],
    'conversion_percent': None, 'conversion_basis': '',
    'feed_total': None, 'feed_unit': '',
    'feed_temperature': 520, 'feed_temperature_unit': '℃',
    'feed_pressure': None, 'feed_pressure_unit': '',
    'case_pressures': [13.5, 13.5], 'case_pressure_unit': 'bar',
    'outlet_temperatures': [710, 600], 'outlet_temperature_unit': '°C',
    'missing_information': ['进料流量由用户自定'],
}

GASIFICATION_FACTS = {
    'species': ['碳', '水', '一氧化碳', '氢气', '二氧化碳', '甲烷'],
    'feed_composition': [{'name': '煤炭', 'fraction': 62.0},
                         {'name': '水', 'fraction': 38.0}],
    'composition_basis': 'mass_percent',
    'reactions': [{'name': '气化',
                   'species': [{'name': '碳', 'coefficient': -1},
                                 {'name': '水', 'coefficient': -1},
                   {'name': '一氧化碳', 'coefficient': 1},
                                {'name': '氢气', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': None, 'conversion_basis': '',
    'feed_total': 80000, 'feed_unit': 'Nm3/h',
    'feed_temperature': 40, 'feed_temperature_unit': '摄氏度',
    'feed_pressure': 40, 'feed_pressure_unit': 'bar',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [1400], 'outlet_temperature_unit': '度',
    'missing_information': [],
}


def ids(questions):
    return [q.id for q in questions]


class UnitNormalisation(unittest.TestCase):

    def test_chinese_temperature_units_are_mapped(self):
        request, report = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        self.assertEqual(request.feeds[0].temperature_unit, 'C')
        self.assertTrue(any('temperature unit' in a for a in report.applied))

    def test_gasification_units_are_mapped(self):
        request, _ = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertEqual(request.feeds[0].temperature_unit, 'C')
        self.assertEqual(request.feeds[0].pressure_unit, 'bar')
        self.assertEqual(request.operating_cases[0].outlet_temperature_unit, 'C')

    def test_the_change_is_recorded_so_it_can_be_reported(self):
        _, report = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        self.assertTrue(report.applied)


class SpeciesResolution(unittest.TestCase):

    def test_chinese_names_reach_the_library(self):
        self.assertEqual(resolve_species('甲苯'), 'Toluene')
        self.assertEqual(resolve_species('甲烷'), 'Methane')
        self.assertEqual(resolve_species('水蒸气'), 'Water')
        self.assertEqual(resolve_species('一氧化碳'), 'CO')

    def test_english_names_still_work(self):
        self.assertEqual(resolve_species('Toluene'), 'Toluene')

    def test_an_ambiguous_name_is_not_guessed(self):
        """Xylene has three isomers; picking one would be a silent wrong choice."""
        self.assertIsNone(resolve_species('二甲苯'))
        self.assertNotIn('二甲苯', SPECIES_ALIASES)

    def test_the_alias_table_is_not_silently_wrong(self):
        for alias, target in SPECIES_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIsNotNone(resolve_species(target),
                                     'alias %r points at unmappable %r' % (alias, target))

    def test_unknown_components_become_a_question_not_a_dropped_field(self):
        facts = dict(TOLUENE_FACTS, species=['甲苯', '未知物质'])
        request, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertIn('Toluene', request.components)
        self.assertTrue(any(q.id.startswith('q-component') for q in report.questions),
                        ids(report.questions))
        self.assertTrue(any('未知物质' in q.question for q in report.questions))
        self.assertTrue(any(q.blocking for q in report.questions
                            if q.id.startswith('q-component')))


class PressureFromOperatingCases(unittest.TestCase):

    def test_a_uniform_per_case_pressure_becomes_the_feed_pressure(self):
        """Reforming states 13.5 bar for each case rather than for the feed."""
        request, report = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertEqual(request.feeds[0].pressure, 13.5)
        self.assertEqual(request.feeds[0].pressure_unit, 'bar')
        self.assertTrue(any('per-case pressure' in a for a in report.applied))
        self.assertNotIn('q-feed-pressure', ids(report.questions))

    def test_a_varying_per_case_pressure_raises_a_question(self):
        facts = dict(SMR_FACTS, case_pressures=[13.5, 20.0])
        request, report = normalize(facts, 'text', scenario_label='smr')
        self.assertIsNone(request.feeds[0].pressure)
        self.assertIn('q-pressure-varies', ids(report.questions))


class VolumetricFlowIsNotConverted(unittest.TestCase):
    """The central refusal: Nm3/h cannot become kg/h without choosing an assumption."""

    def test_the_unit_survives_verbatim(self):
        request, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertEqual(request.feeds[0].total_flow, 80000.0)
        self.assertEqual(request.feeds[0].total_flow_unit, 'Nm3/h')

    def test_a_blocking_question_is_raised(self):
        _, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertIn('q-volumetric-flow', ids(report.questions))
        question = next(q for q in report.questions if q.id == 'q-volumetric-flow')
        self.assertTrue(question.blocking)
        self.assertIn('80000', question.question)

    def test_no_mass_unit_appears_anywhere(self):
        request, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        blob = '%s %s' % (request.feeds[0].total_flow_unit, report.applied)
        self.assertNotIn('kg/h', blob)

    def test_the_substitution_is_recorded_as_such(self):
        _, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertTrue(any('NOT converted' in a for a in report.applied))


KINETIC_FACTS = {
    'species': ['A', 'B'],
    'feed_composition': [{'name': 'A', 'fraction': 1.0}],
    'composition_basis': 'pure',
    'reactions': [{'name': 'r',
                   'species': [{'name': 'A', 'coefficient': -1},
                               {'name': 'B', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': None, 'conversion_basis': '',
    'rate_law': '-rA = k*CA', 'pre_exponential': 1.5e6,
    'activation_energy': 75, 'activation_energy_unit': 'kJ/mol',
    'reaction_order': [{'name': 'A', 'order': 1}],
    'reactor_volume': 2.5, 'reactor_volume_unit': 'm3',
    'residence_time': None, 'residence_time_unit': '',
    'catalyst_mass': None, 'catalyst_mass_unit': '',
    'phase': 'liquid',
    'feed_total': 1000, 'feed_unit': 'kg/h',
    'feed_temperature': 150, 'feed_temperature_unit': 'C',
    'feed_pressure': 5, 'feed_pressure_unit': 'bar',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [], 'outlet_temperature_unit': '',
    'missing_information': [],
}


class KineticsAreNotLost(unittest.TestCase):
    """A request that states a rate law must not be read as "no kinetics".

    `ProcessRequest.kinetic_data` existed and `has_kinetics()` was correct, but
    nothing ever populated the field and nothing extracted the inputs, so it was
    permanently None. A request giving a rate equation, parameters and a reactor
    volume was therefore modelled as a Conversion reactor - the silent substitution
    the selection rules exist to prevent.
    """

    def test_the_rate_law_survives(self):
        request, _ = normalize(KINETIC_FACTS, 'text', scenario_label='kin',
                               phase='mixed', feed_basis='mass_fraction')
        self.assertIsNotNone(request.kinetic_data)
        self.assertTrue(request.has_kinetics())
        self.assertIn('CA', request.kinetic_data.rate_law)

    def test_the_equipment_size_survives(self):
        request, _ = normalize(KINETIC_FACTS, 'text', scenario_label='kin',
                               phase='mixed', feed_basis='mass_fraction')
        self.assertEqual(request.kinetic_data.reactor_volume_m3, 2.5)

    def test_kj_per_mol_becomes_j_per_mol(self):
        """The contract stores J/mol; a person writes kJ/mol."""
        request, _ = normalize(KINETIC_FACTS, 'text', scenario_label='kin',
                               phase='mixed', feed_basis='mass_fraction')
        self.assertEqual(request.kinetic_data.activation_energy_J_mol, 75000.0)

    def test_the_phase_comes_from_the_request(self):
        request, _ = normalize(KINETIC_FACTS, 'text', scenario_label='kin',
                               phase='mixed', feed_basis='mass_fraction')
        self.assertEqual(request.phase, 'liquid')

    def test_selection_refuses_rather_than_substituting(self):
        """CSTR/PFR are not implemented; the answer is 'unsupported', not Conversion."""
        from reactor_agent.selection import select_reactor
        request, _ = normalize(KINETIC_FACTS, 'text', scenario_label='kin',
                               phase='mixed', feed_basis='mass_fraction')
        decision = select_reactor(request, solid_phase=request.has_solid_reactant)
        self.assertEqual(decision.preferred_reactor, 'cstr')
        self.assertIsNone(decision.execution_reactor)
        self.assertEqual(decision.capability_status, 'unsupported')

    def test_no_kinetics_stays_none(self):
        request, _ = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        self.assertIsNone(request.kinetic_data)
        self.assertFalse(request.has_kinetics())


class SolidPhaseIsCarried(unittest.TestCase):
    """Gasification names coal, which `normalize` represents as solid carbon.

    Solid carbon no longer makes the combination experimental: the accepted
    saturated-carbon route covers it, so the capability is now verified. The flag
    still has to reach the lookup, because that is what selects the saturation
    route rather than a plain Gibbs case.
    """

    def test_a_solid_reactant_is_detected(self):
        request, _ = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertTrue(request.has_solid_reactant)

    def test_the_capability_is_the_saturation_route(self):
        from reactor_agent.selection import select_reactor
        request, _ = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        decision = select_reactor(request, solid_phase=request.has_solid_reactant)
        self.assertEqual(decision.capability_status, 'verified')

    def test_it_is_declared_as_an_assumption(self):
        _, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertTrue(any(a.id == 'a-solid-phase' for a in report.assumptions))

    def test_a_gas_phase_case_without_solids_is_unaffected(self):
        request, _ = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertFalse(request.has_solid_reactant)


class ThermalBoundaryIsDeclared(unittest.TestCase):
    """Choosing the thermal boundary is a decision, so it is stated as one."""

    def _plan(self, facts, text, label):
        from reactor_agent.pipeline import build_plan
        from reactor_agent.selection import select_reactor
        request, report = normalize(facts, text, scenario_label=label,
                                    phase='mixed', feed_basis='mass_fraction')
        decision = select_reactor(request, solid_phase=request.has_solid_reactant)
        return build_plan(request, decision, report)

    def test_a_defaulted_adiabatic_case_is_declared(self):
        plan = self._plan(TOLUENE_FACTS, TOLUENE_TEXT, 't')
        self.assertEqual(plan.thermal_mode, 'adiabatic')
        self.assertTrue(any(a.id == 'a-thermal-mode' for a in plan.assumptions))

    def test_a_stated_outlet_temperature_is_declared_as_isothermal(self):
        plan = self._plan(SMR_FACTS, 'text', 'smr')
        self.assertEqual(plan.thermal_mode, 'isothermal')
        entry = next(a for a in plan.assumptions if a.id == 'a-thermal-mode')
        self.assertEqual(entry.source, 'agent_default')
        self.assertIn('等温', entry.scope)

    def test_an_explicit_mode_is_not_declared_as_ours(self):
        from reactor_agent.pipeline import build_plan
        from reactor_agent.selection import select_reactor
        request, report = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        decision = select_reactor(request)
        plan = build_plan(request, decision, report, heat_mode='isothermal')
        self.assertFalse(any(a.id == 'a-thermal-mode' for a in plan.assumptions))


class Reactions(unittest.TestCase):

    def test_reactants_are_negative_and_products_positive(self):
        request, _ = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        stoich = request.reactions[0].stoichiometry
        self.assertLess(stoich['Toluene'], 0)
        self.assertGreater(stoich['Benzene'], 0)

    def test_the_signs_are_taken_as_given(self):
        """One signed list: a negative coefficient IS the reactant marker."""
        facts = dict(TOLUENE_FACTS, reactions=[{
            'name': 'r', 'species': [{'name': '甲苯', 'coefficient': -2},
                                     {'name': '苯', 'coefficient': 1}],
            'reversible': False}])
        request, _ = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertAlmostEqual(request.reactions[0].stoichiometry['Toluene'], -2.0)

    def test_a_reaction_with_no_product_is_a_question(self):
        """The real failure: a model put every species in `reactants`, so the
        reaction had nothing on the product side and balanced to zero."""
        facts = dict(TOLUENE_FACTS, reactions=[{
            'name': '歧化',
            'species': [{'name': '甲苯', 'coefficient': -2},
                        {'name': '苯', 'coefficient': -1},
                        {'name': '邻二甲苯', 'coefficient': -1}],
            'reversible': False}])
        _, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertIn('q-reaction-no-product', ids(report.questions))
        self.assertTrue(any(q.blocking for q in report.questions
                            if q.id == 'q-reaction-no-product'))

    def test_a_reaction_with_no_reactant_is_a_question(self):
        facts = dict(TOLUENE_FACTS, reactions=[{
            'name': 'r', 'species': [{'name': '苯', 'coefficient': 1}],
            'reversible': False}])
        _, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertIn('q-reaction-no-reactant', ids(report.questions))

    def test_a_zero_coefficient_is_dropped(self):
        facts = dict(TOLUENE_FACTS, reactions=[{
            'name': 'r',
            'species': [{'name': '甲苯', 'coefficient': -2},
                        {'name': '苯', 'coefficient': 1},
                        {'name': '水', 'coefficient': 0}],
            'reversible': False}])
        request, _ = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertNotIn('Water', request.reactions[0].stoichiometry)

    def test_a_gibbs_case_may_have_no_reactions(self):
        request, _ = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertEqual(request.reactions, [])


class Conversion(unittest.TestCase):

    def test_the_basis_is_kept(self):
        request, _ = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        self.assertEqual(request.conversion_constraints[0].base_component, 'Toluene')
        self.assertEqual(request.conversion_constraints[0].percent, 50.0)

    def test_the_basis_is_inferred_when_there_is_one_reactant(self):
        facts = dict(TOLUENE_FACTS, conversion_basis='')
        request, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertEqual(request.conversion_constraints[0].base_component, 'Toluene')
        self.assertTrue(any('inferred' in a for a in report.applied))

    def test_an_unknown_basis_is_a_question(self):
        facts = dict(TOLUENE_FACTS, conversion_basis='',
                     reactions=[{'name': 'r',
                                 'species': [{'name': '甲苯', 'coefficient': -1},
                                               {'name': '水', 'coefficient': -1},
                                 {'name': '苯', 'coefficient': 1}],
                                 'reversible': False}])
        _, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertIn('q-conversion-basis', ids(report.questions))


class MissingValuesBecomeQuestions(unittest.TestCase):

    def test_absent_required_values_are_never_filled_in(self):
        facts = dict(TOLUENE_FACTS, feed_total=None, feed_pressure=None,
                     feed_temperature=None)
        request, report = normalize(facts, TOLUENE_TEXT, scenario_label='t')
        self.assertIsNone(request.feeds[0].total_flow)
        self.assertIsNone(request.feeds[0].pressure)
        self.assertIsNone(request.feeds[0].temperature)
        for expected in ('q-feed-flow', 'q-feed-pressure', 'q-feed-temperature'):
            self.assertIn(expected, ids(report.questions))

    def test_the_reforming_flow_stays_absent_when_it_was_not_delegated(self):
        """Without the exam's "可以自定" wording, a missing flow is simply missing."""
        request, _ = normalize(SMR_FACTS, 'text with no delegation',
                               scenario_label='smr')
        self.assertIsNone(request.feeds[0].total_flow)

    def test_what_the_model_flagged_as_unstated_is_carried_into_the_report(self):
        _, report = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertTrue(any('进料流量由用户自定' in note for note in report.notes))


class DelegatedChoices(unittest.TestCase):
    """Scenario 1 says the flow "可以自定" - an instruction to choose, not a gap."""

    DELEGATING = ('我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7），'
                  '进料流量可以自定，要求符合一个工厂一年正常的处理量。'
                  '出口温度 710°C，压力 13.5 bar，进料温度 520℃。')

    def test_the_delegation_is_detected(self):
        self.assertTrue(flow_is_ours_to_choose(self.DELEGATING))
        self.assertFalse(flow_is_ours_to_choose(TOLUENE_TEXT))

    def test_a_choice_is_made_and_recorded_as_an_assumption(self):
        request, report = normalize(SMR_FACTS, self.DELEGATING, scenario_label='smr')
        self.assertIsNotNone(request.feeds[0].total_flow)
        self.assertEqual(len(report.agent_choices), 1)
        assumption = report.agent_choices[0]
        self.assertEqual(assumption.source, 'agent_default')
        self.assertIn('可以自定', assumption.scope)

    def test_no_question_is_asked_about_a_value_we_were_told_to_pick(self):
        _, report = normalize(SMR_FACTS, self.DELEGATING, scenario_label='smr')
        self.assertNotIn('q-feed-flow', ids(report.questions))

    def test_the_stated_ratio_is_preserved_by_the_choice(self):
        """The chosen total must keep CH4:H2O at 1:2.7."""
        request, _ = normalize(SMR_FACTS, self.DELEGATING, scenario_label='smr')
        self.assertTrue(request.feeds[0].fractions)
        ratio = (request.feeds[0].fractions['Water']
                 / request.feeds[0].fractions['Methane'])
        self.assertAlmostEqual(ratio, 2.7, places=6)

    def test_the_choice_is_an_assumption_not_an_extraction(self):
        """It must never be presented as something the user said."""
        request, report = normalize(SMR_FACTS, self.DELEGATING, scenario_label='smr')
        self.assertTrue(any('chosen by us' in a for a in report.applied))
        # And there is no question implying we could not proceed.
        self.assertFalse(any(q.field == 'feeds[0].total_flow'
                             for q in report.blocking))


class OperatingCases(unittest.TestCase):

    def test_each_outlet_temperature_becomes_its_own_case(self):
        request, _ = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertEqual([c.outlet_temperature for c in request.operating_cases],
                         [710.0, 600.0])
        self.assertTrue(all(c.thermal_mode == 'isothermal'
                            for c in request.operating_cases))

    def test_case_ids_are_distinct(self):
        request, _ = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertEqual(len({c.case_id for c in request.operating_cases}),
                         len(request.operating_cases))


class NormalVolumeIsConfirmedBeforeItIsUsed(unittest.TestCase):
    """Nm3/h gets a suggested answer, and only the answer unlocks the path.

    The plan's central rule still holds: without the user's confirmation nothing is
    converted and nothing is assumed. What changes is that there is now a default
    answer to confirm, and confirming it hands the tool layer's own normal-volume
    input a complete basis instead of an unanswerable question.
    """

    CONFIRMED_BASIS = {'standard_temperature_C': 0.0,
                       'standard_pressure_kPa': 101.325}

    def _confirmed(self):
        facts = dict(GASIFICATION_FACTS,
                     normal_volume_basis=dict(self.CONFIRMED_BASIS))
        return normalize(facts, TOLUENE_TEXT, scenario_label='g')

    def test_the_question_offers_the_documented_default(self):
        _, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        question = next(q for q in report.questions if q.id == 'q-volumetric-flow')
        self.assertEqual(question.default, '总进料，0°C/101.325 kPa')
        self.assertIn('80000', question.question)
        self.assertTrue(question.blocking)

    def test_unconfirmed_answers_stay_local(self):
        request, report = normalize(GASIFICATION_FACTS, 'text', scenario_label='g')
        self.assertEqual(request.feeds[0].flow_input, 'local')
        self.assertIsNone(request.feeds[0].standard_temperature_C)
        self.assertNotIn('a-normal-volume', [a.id for a in report.assumptions])

    def test_a_confirmed_basis_opens_the_normal_volume_path(self):
        request, report = self._confirmed()
        feed = request.feeds[0]
        self.assertEqual(feed.flow_input, 'normal_volume')
        self.assertEqual(feed.standard_temperature_C, 0.0)
        self.assertEqual(feed.standard_pressure_kPa, 101.325)
        self.assertEqual(feed.total_flow_unit, 'Nm3/h')
        self.assertNotIn('q-volumetric-flow', ids(report.questions))
        assumption = next(a for a in report.assumptions if a.id == 'a-normal-volume')
        self.assertEqual(assumption.source, 'user_answer')
        self.assertTrue(assumption.accepted)
        self.assertIn('80000 Nm3/h @ 0°C/101.325 kPa', assumption.value)
        self.assertIn('22.414', assumption.scope)
        self.assertFalse(any('NOT converted' in a for a in report.applied))
        self.assertTrue(any('normal volume confirmed' in a for a in report.applied))

    def test_a_plain_operating_volume_gets_no_default(self):
        """m3/h is a working volume, not a standard one: no suggested answer."""
        facts = dict(GASIFICATION_FACTS, feed_unit='m3/h')
        _, report = normalize(facts, 'text', scenario_label='g')
        question = next(q for q in report.questions if q.id == 'q-volumetric-flow')
        self.assertIsNone(question.default)

    def test_the_confirmed_volume_is_not_converted_here(self):
        request, _ = self._confirmed()
        self.assertEqual(request.feeds[0].total_flow, 80000.0)
        self.assertEqual(request.feeds[0].total_flow_unit, 'Nm3/h')


class TheFlowIsAnchoredOnTheCarbonReactant(unittest.TestCase):
    """Anchoring on the largest fraction gave methane 370 kmol/h instead of 1000."""

    DELEGATING = ('我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7），'
                  '进料流量可以自定，要求符合一个工厂一年正常的处理量。'
                  '出口温度 710°C，压力 13.5 bar，进料温度 520℃。')

    def _request(self):
        request, report = normalize(SMR_FACTS, self.DELEGATING,
                                    scenario_label='smr')
        return request, report

    def test_methane_carries_the_thousand(self):
        request, _ = self._request()
        feed = request.feeds[0]
        self.assertAlmostEqual(feed.total_flow, 3700.0, places=6)
        share = feed.fractions['Methane'] * feed.total_flow
        self.assertAlmostEqual(share, 1000.0, places=6)

    def test_the_scope_states_the_annual_tonnage(self):
        _, report = self._request()
        assumption = next(a for a in report.assumptions if a.id == 'a-feed-flow')
        self.assertIn('128', assumption.scope)
        self.assertIn('8000', assumption.scope)
        self.assertIn('Methane', assumption.scope)


class TheGibbsPlanIsCompleted(unittest.TestCase):
    """A Gibbs reactor only distributes among the candidates it is given.

    The accepted gasification spec carries CO2 and Methane as well as the four species
    the request names, so a request that lists only those four has to be completed -
    and the completion declared, because nothing in the request asked for it.
    """

    THIN_FACTS = dict(GASIFICATION_FACTS,
                      species=['碳', '水', '一氧化碳', '氢气'])

    def _plan(self, facts=None):
        from reactor_agent.pipeline import build_plan
        from reactor_agent.selection import select_reactor
        request, report = normalize(facts or GASIFICATION_FACTS, 'text',
                                    scenario_label='g')
        decision = select_reactor(request, solid_phase=request.has_solid_reactant)
        return request, build_plan(request, decision, report)

    def test_the_candidates_are_completed(self):
        _, plan = self._plan(self.THIN_FACTS)
        self.assertIn('CO2', plan.components)
        self.assertIn('Methane', plan.components)

    def test_the_completion_is_an_assumption(self):
        _, plan = self._plan(self.THIN_FACTS)
        assumption = next(a for a in plan.assumptions
                          if a.id == 'a-gibbs-candidates')
        self.assertEqual(assumption.source, 'agent_default')
        self.assertEqual(assumption.value, ['CO2', 'Methane'])
        self.assertFalse(assumption.accepted)

    def test_a_complete_candidate_list_is_not_touched(self):
        _, plan = self._plan()
        self.assertNotIn('a-gibbs-candidates',
                         [a.id for a in plan.assumptions])

    def test_the_saturation_route_is_declared(self):
        _, plan = self._plan()
        assumption = next(a for a in plan.assumptions
                          if a.id == 'a-solid-carbon-route')
        self.assertEqual(assumption.value, 'saturation')
        self.assertEqual(assumption.source, 'derived')
        self.assertTrue(assumption.accepted)
        self.assertIn('LIQUID', assumption.scope)

    def test_the_missing_oxygen_is_noted_without_blocking(self):
        """External heat is a different duty from autothermal gasification."""
        _, plan = self._plan()
        question = next(q for q in plan.questions if q.id == 'q-no-oxygen')
        self.assertFalse(question.blocking)
        self.assertIn('外部供热', question.question)
        self.assertFalse(any(q.id == 'q-no-oxygen' for q in plan.blocking_questions()))


class TheXyleneSplitIsDeclared(unittest.TestCase):
    def test_an_equal_split_written_by_the_model_is_recorded(self):
        """The smoke record shows the model splitting the isomers itself."""
        _, report = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        assumption = next(a for a in report.assumptions if a.id == 'a-isomer-split')
        self.assertEqual(assumption.value, 'o/m/p-Xylene 各 1/3')
        self.assertEqual(assumption.source, 'agent_default')
        self.assertFalse(assumption.accepted)

    def test_it_is_recorded_once_only(self):
        _, report = normalize(TOLUENE_FACTS, TOLUENE_TEXT, scenario_label='t')
        self.assertEqual([a.id for a in report.assumptions].count('a-isomer-split'), 1)

    def test_a_request_without_the_isomers_gets_no_such_assumption(self):
        _, report = normalize(SMR_FACTS, 'text', scenario_label='smr')
        self.assertNotIn('a-isomer-split', [a.id for a in report.assumptions])


class QuestionIdsSurviveAProcessBoundary(unittest.TestCase):
    """`hash()` is salted per process, so ids changed between pause and resume."""

    SCRIPT = ('from reactor_agent.normalize import _stable_id;'
              'print(_stable_id("未知物质"), _stable_id("q-component-未知物质"))')

    def test_the_same_text_gives_the_same_id_under_any_hash_seed(self):
        outputs = []
        for seed in ('1', '2'):
            environment = dict(os.environ, PYTHONHASHSEED=seed)
            result = subprocess.run(
                [sys.executable, '-c', self.SCRIPT], capture_output=True, text=True,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(outputs[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
