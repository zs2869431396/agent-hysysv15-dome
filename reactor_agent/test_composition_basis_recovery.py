"""A feed's explicit composition basis must survive model extraction errors."""
import copy
import json
import unittest
from pathlib import Path

from hysys_tools.core import feed_molar_flows
from hysys_tools.precheck import validate_spec
from reactor_agent.fixtures.recovery_cases import model_client, EMPTY_REVIEW
from reactor_agent.graph import build_graph, initial_state
from reactor_agent.input_recovery import recover_explicit_inputs
from reactor_agent.normalize import normalize


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' /
                      'wgs_mass_basis_failure.json').read_text(encoding='utf-8'))


class CompositionBasisRecoveryTests(unittest.TestCase):
    def test_real_failed_extraction_compiles_both_temperatures_with_correct_feed(self):
        facts = copy.deepcopy(FIXTURE['facts'])
        client, calls = model_client(facts, EMPTY_REVIEW)
        graph = build_graph(client)
        state = graph.invoke(initial_state(FIXTURE['text']),
                             {'configurable': {'thread_id': 'wgs-basis'}})
        self.assertEqual(state['status'], 'READY', state['problems'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['facts']['composition_basis'], 'molar_fraction')
        self.assertEqual(state['review']['original_facts']['composition_basis'], 'mass_fraction')
        correction = next(r for r in state['review']['source_recoveries']
                          if r['field'] == 'composition_basis')
        self.assertIn(correction['evidence'], FIXTURE['text'])
        self.assertEqual(len(state['cases']), 2)
        self.assertEqual([c['spec']['reactor']['outlet_temperature'] for c in state['cases']],
                         [350, 500])
        for case in state['cases']:
            spec = case['spec']
            self.assertTrue(validate_spec(spec)['ok'])
            feed = spec['feeds'][0]
            self.assertEqual(feed['total_flow_unit'], 'kmol/h')
            self.assertEqual(feed_molar_flows(feed), {'CO': 400, 'Water': 600})
        self.assertEqual(facts['composition_basis'], 'mass_fraction')

    def test_normalizer_also_recovers_basis_without_optional_second_model(self):
        request, report = normalize(FIXTURE['facts'], source_text=FIXTURE['text'])
        self.assertEqual(request.feeds[0].basis, 'molar_fraction')
        self.assertEqual(request.feeds[0].fractions, {'CO': .4, 'Water': .6})
        self.assertEqual(len(request.operating_cases), 2)
        self.assertTrue(any('composition_basis' in item for item in report.applied))

    def test_explicit_mass_composition_overrides_wrong_molar_basis(self):
        facts = {'composition_basis': 'mole_fraction', 'feed_unit': 'kg/h'}
        repaired, records = recover_explicit_inputs('进料1000 kg/h，质量组成为CO 40%、水60%。', facts)
        self.assertEqual(repaired['composition_basis'], 'mass_fraction')
        self.assertEqual(records[-1]['evidence'], '质量组成')

    def test_total_flow_unit_and_product_split_do_not_choose_feed_basis(self):
        facts = {'composition_basis': 'mass_fraction', 'feed_unit': 'kmol/h'}
        repaired, records = recover_explicit_inputs(
            '进料总流量1000 kmol/h。产物摩尔组成为CO 40%、水60%。', facts)
        self.assertEqual(repaired, facts)
        self.assertEqual(records, [])

    def test_conflicting_feed_composition_labels_are_not_resolved_by_guessing(self):
        facts = {'composition_basis': 'mass_fraction'}
        repaired, records = recover_explicit_inputs(
            '进料摩尔组成为CO 40%、水60%。进料质量组成为CO 40%、水60%。', facts)
        self.assertEqual(repaired, facts)
        self.assertEqual(records, [])

    def test_english_feed_mole_fraction_is_recovered_from_literal_label(self):
        repaired, records = recover_explicit_inputs(
            'Feed mole fractions: CO 40%, H2O 60%.', {'composition_basis': None})
        self.assertEqual(repaired['composition_basis'], 'molar_fraction')
        self.assertEqual(records[-1]['evidence'], 'mole fractions')


if __name__ == '__main__':
    unittest.main()
