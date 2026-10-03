"""Tests for fact extraction and, above all, for the grounding check.

The grounding check is the layer that catches a well-formed invention, so it is
tested against failures that actually happened during model selection:

  * a candidate filled in `100 mol/s` for a flow the exam explicitly leaves to us;
  * a candidate returned `0.5` for "转化率 50%" (percentage read as a fraction);
  * a value stated per operating case was dropped because no field matched it.

Everything is offline: the model client is given a fake transport.
"""
from __future__ import annotations

import json
import unittest

from reactor_agent.extraction import (
    EXTRACTION_SCHEMA,
    REQUIRED_BY_KIND,
    Extraction,
    extract,
    extract_verified,
    grounding_failures,
    reaction_grounding_failures,
    reaction_is_derived,
    states_numeric_equation,
    written_equations,
)
from reactor_agent.llm import ChatClient, LlmConfig

TOLUENE = (
    '请帮我完成甲苯歧化反应的模拟，甲苯进料流量10000kg/h，进料温度为380℃，'
    '操作压力2.5MPa，甲苯转化率为50%，反应产物为苯和二甲苯（邻、间、对三种异构体）')

SMR = (
    '我需要模拟甲烷蒸汽重整。进料是甲烷和水蒸气（摩尔比 1:2.7），'
    '1、重整炉出口气温度为 710°C，压力 13.5 bar，进料温度520℃；'
    '2、重整炉出口气温度为600℃，压力13.5bar，进料温度520℃。'
    '进料流量可以自定，要求符合一个工厂一年正常的处理量')


def reply(content: str) -> str:
    return json.dumps({'choices': [{'finish_reason': 'stop',
                                    'message': {'content': content}}],
                       'usage': {}})


def fake_client(bodies):
    config = LlmConfig(base='https://example.test/v1', key='sk-' + 't' * 30,
                       min_interval=0)
    queue = [(200, reply(body)) for body in bodies]

    def transport(url, payload, headers, timeout):
        return queue.pop(0)

    return ChatClient(config, transport=transport, sleeper=lambda _s: None)


class WrittenEquationsAreTheOnlyEvidence(unittest.TestCase):
    """Only a formula on both sides of an arrow counts as a written equation.

    The old test was `\\d\\s*[A-Z][a-z]?`, which matched the `2O` in `H2O`, the `5M` in
    `2.5MPa` and the `0N` in `80000Nm3/h`. Nearly every request therefore looked like
    one where the user had written coefficients, and the reforming request's
    element-balanced 3H2 was reported as an unsupported coefficient - a blocking
    question about a number the model had derived correctly.
    """

    def test_a_formula_equation_is_read_with_its_coefficients(self):
        self.assertEqual(written_equations('甲苯 2C₇H₈ → C₆H₆ + C₈H₁₀'),
                         [{'C7H8': -2.0, 'C6H6': 1.0, 'C8H10': 1.0}])

    def test_implicit_coefficients_count_as_one(self):
        equations = written_equations('主要反应：C+H2O → CO+H2')
        self.assertEqual(len(equations), 1)
        self.assertEqual(sorted(abs(v) for v in equations[0].values()),
                         [1.0, 1.0, 1.0, 1.0])

    def test_an_equals_sign_is_an_arrow(self):
        self.assertEqual(len(written_equations('C + H2O = CO + H2')), 1)

    def test_a_ratio_and_units_are_not_an_equation(self):
        for text in ('进料是甲烷和水蒸气（摩尔比 1:2.7），压力 13.5 bar，H2O',
                     '流量80000Nm3/h，压力2.5MPa',
                     'Kp=2.3，T=380'):
            with self.subTest(text=text):
                self.assertEqual(written_equations(text), [])
                self.assertFalse(states_numeric_equation(text))

    def test_a_written_equation_is_detected(self):
        self.assertTrue(states_numeric_equation('C+H2O → CO+H2'))

    # ------------------------------------------------------- coefficient checks
    def test_a_derived_coefficient_is_not_flagged_when_no_equation_was_written(self):
        """The reforming defect: 3H2 came from the element balance, not from a claim."""
        reactions = [{'name': 'SMR', 'species': [
            {'name': 'H2', 'coefficient': 3},
            {'name': 'CO', 'coefficient': 1}]}]
        self.assertEqual(
            reaction_grounding_failures(reactions, '甲烷和水蒸气反应生成一氧化碳和氢气'),
            [])
        self.assertTrue(reaction_is_derived(reactions[0],
                                            '甲烷和水蒸气反应生成一氧化碳和氢气'))

    def test_a_coefficient_with_no_equation_to_support_it_is_flagged(self):
        reactions = [{'name': 'D', 'species': [{'name': '苯', 'coefficient': 3}]}]
        self.assertEqual(
            reaction_grounding_failures(reactions, '甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀'),
            ['reactions[D].苯'])
        self.assertFalse(reaction_is_derived(reactions[0],
                                             '甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀'))

    def test_the_equations_own_coefficients_justify_a_model_coefficient(self):
        reactions = [{'name': 'D', 'species': [{'name': 'C8H10', 'coefficient': 2}]}]
        self.assertEqual(
            reaction_grounding_failures(reactions, '甲苯 2C₇H₈ → C₆H₆ + 2C₈H₁₀'), [])


class Grounding(unittest.TestCase):

    def test_numbers_the_user_stated_are_accepted(self):
        facts = {'feed_total': 10000.0, 'feed_temperature': 380.0,
                 'feed_pressure': 2.5, 'conversion_percent': 50.0}
        self.assertEqual(grounding_failures(facts, TOLUENE), [])

    def test_an_invented_number_is_caught(self):
        """The real case: the exam leaves the flow free, a model chose 100 mol/s."""
        facts = {'feed_total': 100.0, 'feed_temperature': 520.0,
                 'feed_pressure': 13.5}
        failures = grounding_failures(facts, SMR)
        self.assertIn('feed_total', failures)

    def test_a_field_may_be_exempted_when_the_exam_allows_a_choice(self):
        facts = {'feed_total': 100.0}
        self.assertEqual(grounding_failures(facts, SMR, allowed={'feed_total'}), [])

    def test_a_percentage_read_as_a_fraction_is_caught(self):
        """A real failure: 50% came back as 0.5, and a unit-factor rule would allow it."""
        facts = {'conversion_percent': 0.5}
        self.assertIn('conversion_percent',
                      grounding_failures(facts, TOLUENE))

    def test_a_small_number_without_a_percent_sign_is_not_misflagged(self):
        """The percent guard must be narrow or it would reject ordinary values."""
        text = '进料为 0.5 kg/h 的示踪剂'
        self.assertEqual(grounding_failures({'feed_total': 0.5}, text), [])

    def test_a_value_in_another_unit_still_counts_as_grounded(self):
        """2.5 MPa and 2500 kPa are the same statement."""
        self.assertEqual(grounding_failures({'feed_pressure': 2500.0}, TOLUENE), [])

    def test_per_case_pressures_are_checked_elementwise(self):
        facts = {'case_pressures': [13.5, 13.5]}
        self.assertEqual(grounding_failures(facts, SMR), [])
        self.assertIn('case_pressures[1]',
                      grounding_failures({'case_pressures': [13.5, 99.0]}, SMR))

    def test_empty_values_are_not_reported_as_invented(self):
        """Absent is a different condition from wrong - it becomes a question."""
        facts = {'feed_total': None, 'feed_pressure': None}
        self.assertEqual(grounding_failures(facts, SMR), [])

    def test_a_non_numeric_value_is_reported(self):
        self.assertIn('feed_total', grounding_failures({'feed_total': 'lots'}, SMR))


class ScenarioSpecificRequirements(unittest.TestCase):

    def test_a_conversion_case_needs_a_conversion_figure(self):
        self.assertIn('conversion_percent',
                      Extraction(facts={}).gaps('conversion'))

    def test_a_gibbs_case_does_not(self):
        """Gibbs needs no conversion figure; demanding one fails correct behaviour."""
        self.assertNotIn('conversion_percent', Extraction(facts={}).gaps('gibbs'))
        self.assertEqual(Extraction(facts={}).gaps('gibbs'),
                         ['feed_temperature', 'feed_pressure', 'outlet_temperatures'])

    def test_the_required_sets_differ_by_scenario(self):
        self.assertNotEqual(REQUIRED_BY_KIND['conversion'], REQUIRED_BY_KIND['gibbs'])


class SchemaShape(unittest.TestCase):

    def test_no_open_dictionaries(self):
        """`additionalProperties` as a schema would let (and force) the model to
        invent key names - the cause of the mangled-keys incident."""
        def walk(node, path='$'):
            if isinstance(node, dict):
                if 'additionalProperties' in node and isinstance(
                        node['additionalProperties'], dict):
                    self.fail('open dictionary at %s' % path)
                for key, value in node.items():
                    walk(value, '%s.%s' % (path, key))
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, '%s[%d]' % (path, index))
        walk(EXTRACTION_SCHEMA)

    def test_pressure_can_be_stated_per_operating_case(self):
        """Without this field the model silently dropped 13.5 bar."""
        self.assertIn('case_pressures', EXTRACTION_SCHEMA['properties'])

    def test_there_is_a_place_to_record_what_was_not_stated(self):
        self.assertIn('missing_information', EXTRACTION_SCHEMA['properties'])


class RequirementsFollowSelection(unittest.TestCase):
    """Which fields are required depends on the reactor, which is chosen after reading.

    Requiring a conversion figure before anything had been selected asked a Gibbs
    request for a number it could never have, and a custom `--text` entry point
    defaulted every request to Conversion - so the same reforming input that worked as
    a scenario failed through the free-text entry.
    """

    def test_no_kind_means_nothing_is_required_yet(self):
        self.assertEqual(Extraction(facts={}).gaps(''), [])
        self.assertEqual(Extraction(facts={}).gaps(None), [])

    def test_a_conversion_request_needs_a_conversion_figure(self):
        self.assertIn('conversion_percent',
                      Extraction(facts={}).gaps('conversion'))

    def test_a_gibbs_request_does_not(self):
        self.assertNotIn('conversion_percent', Extraction(facts={}).gaps('gibbs'))

    def test_the_kind_follows_the_selected_reactor(self):
        from reactor_agent.extraction import required_kind_for
        self.assertEqual(required_kind_for('gibbs'), 'gibbs')
        self.assertEqual(required_kind_for('equilibrium'), 'gibbs')
        self.assertEqual(required_kind_for('conversion'), 'conversion')
        # CSTR/PFR need kinetics and volume, not a conversion figure.
        self.assertEqual(required_kind_for('cstr'), '')
        self.assertEqual(required_kind_for('pfr'), '')
        self.assertEqual(required_kind_for(None), '')

    def test_check_required_reports_only_for_a_known_kind(self):
        from reactor_agent.extraction import check_required
        extraction = Extraction(facts={})
        self.assertEqual(check_required(extraction, ''), [])
        self.assertTrue(check_required(extraction, 'conversion'))
        self.assertNotIn('conversion_percent',
                         ' '.join(check_required(extraction, 'gibbs')))

    def test_extraction_without_a_kind_does_not_retry_on_gaps(self):
        """A Gibbs request must not be retried three times for a conversion figure."""
        c = fake_client([json.dumps({
            'feed_temperature': 520, 'feed_pressure': 13.5,
            'feed_temperature_unit': 'C', 'feed_pressure_unit': 'bar',
            'outlet_temperatures': [710], 'species': [], 'case_pressures': [],
            'feed_composition': [], 'missing_information': []})] * 4)
        result = extract(c, SMR)          # no kind
        self.assertEqual(result.attempts, 1)


class ExtractionCalls(unittest.TestCase):

    def test_a_good_reply_is_returned_directly(self):
        c = fake_client([json.dumps({
            'feed_total': 10000, 'feed_unit': 'kg/h', 'feed_temperature': 380,
            'feed_temperature_unit': 'C', 'feed_pressure': 2.5,
            'feed_pressure_unit': 'MPa', 'conversion_percent': 50,
            'species': [], 'outlet_temperatures': [], 'case_pressures': [],
            'missing_information': []})])
        result = extract(c, TOLUENE)
        self.assertIsNone(result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.get('conversion_percent'), 50)
        self.assertEqual(result.gaps('conversion'), [])

    def test_empty_required_values_are_retried(self):
        """The gaps are random, so asking again is worth it."""
        incomplete = json.dumps({'feed_total': None, 'feed_temperature': None,
                                 'feed_pressure': None, 'conversion_percent': None,
                                 'species': [], 'outlet_temperatures': [],
                                 'case_pressures': [], 'missing_information': []})
        complete = json.dumps({'feed_total': 10000, 'feed_temperature': 380,
                               'feed_pressure': 2.5, 'conversion_percent': 50,
                               'species': [], 'outlet_temperatures': [],
                               'case_pressures': [], 'missing_information': []})
        c = fake_client([incomplete, complete])
        result = extract(c, TOLUENE, kind='conversion')
        self.assertIsNone(result.error)
        self.assertEqual(result.get('conversion_percent'), 50)
        self.assertGreaterEqual(result.attempts, 2)

    def test_retry_is_skipped_when_no_scenario_is_given(self):
        """Without a scenario the required set is unknown, so the first reply stands."""
        incomplete = json.dumps({'feed_total': None, 'conversion_percent': None,
                                 'species': [], 'outlet_temperatures': [],
                                 'case_pressures': [], 'missing_information': []})
        c = fake_client([incomplete])
        result = extract(c, TOLUENE)
        self.assertEqual(result.attempts, 1)

    def test_a_persistent_call_failure_is_reported_not_raised(self):
        c = fake_client(['not json at all'] * 8)
        result = extract(c, TOLUENE, kind='conversion')
        self.assertIsNotNone(result.error)
        self.assertEqual(result.gaps('conversion'),
                         list(REQUIRED_BY_KIND['conversion']))

    def test_verified_extraction_reports_both_kinds_of_problem(self):
        # Retry is on, so the same defective reply is served repeatedly.
        c = fake_client([json.dumps({
            'feed_total': 999, 'feed_temperature': None, 'feed_pressure': None,
            'conversion_percent': 0.5, 'species': [], 'outlet_temperatures': [],
            'case_pressures': [], 'missing_information': []})] * 8)
        _, problems = extract_verified(c, TOLUENE, 'conversion')
        joined = ' | '.join(problems)
        self.assertIn('feed_temperature', joined)      # missing
        self.assertIn('feed_total', joined)            # invented
        self.assertIn('conversion_percent', joined)    # percent read as a fraction

    def test_a_clean_extraction_reports_no_problems(self):
        c = fake_client([json.dumps({
            'feed_total': 10000, 'feed_unit': 'kg/h', 'feed_temperature': 380,
            'feed_temperature_unit': 'C', 'feed_pressure': 2.5,
            'feed_pressure_unit': 'MPa', 'conversion_percent': 50, 'species': [],
            'outlet_temperatures': [], 'case_pressures': [],
            'missing_information': []})] * 2)
        _, problems = extract_verified(c, TOLUENE, 'conversion')
        self.assertEqual(problems, [])

    def test_what_the_model_says_was_unstated_is_kept(self):
        c = fake_client([json.dumps({
            'feed_total': None, 'feed_temperature': 520, 'feed_pressure': None,
            'conversion_percent': None, 'species': [], 'outlet_temperatures': [710],
            'case_pressures': [13.5],
            'missing_information': ['煤的组成', 'Nm3/h 的标准状态']})])
        result = extract(c, SMR)
        self.assertEqual(result.missing_information, ['煤的组成', 'Nm3/h 的标准状态'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
