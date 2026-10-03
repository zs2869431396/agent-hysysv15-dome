"""Offline reproductions of the first workstation Agent pre-check failure."""
import copy
import unittest

from reactor_agent.__main__ import SCENARIOS
from reactor_agent.nodes.plan import make_plan_node
from reactor_agent.nodes.state import initial_state
from reactor_agent.normalize import normalize
from reactor_agent.test_normalize import TOLUENE_FACTS


class WrittenIsomerGroupTotal(unittest.TestCase):
    def facts(self, coefficients=(1, 1, 1)):
        facts = copy.deepcopy(TOLUENE_FACTS)
        for entry, value in zip(facts['reactions'][0]['species'][2:], coefficients):
            entry['coefficient'] = value
        return facts

    def test_each_isomer_cannot_duplicate_the_written_group_total(self):
        request, report = normalize(self.facts(), SCENARIOS['toluene']['text'])
        stoich = request.reactions[0].stoichiometry
        self.assertEqual(stoich['Toluene'], -2)
        self.assertEqual(stoich['Benzene'], 1)
        for name in ('o-Xylene', 'm-Xylene', 'p-Xylene'):
            self.assertAlmostEqual(stoich[name], 1 / 3)
        self.assertTrue(any('group total restored' in note for note in report.applied))
        self.assertEqual([a.id for a in report.assumptions].count('a-isomer-split'), 1)

    def test_the_reproduced_failure_now_compiles_to_ready_without_a_model(self):
        scenario = SCENARIOS['toluene']
        state = initial_state(scenario['text'], kind=scenario['kind'],
                              phase=scenario['phase'], feed_basis=scenario['feed_basis'])
        state['facts'] = self.facts()
        result = make_plan_node()(state)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['blocking'], [])
        self.assertEqual(len(result['cases']), 1)
        self.assertFalse(any('compilation failed' in p for p in result['problems']))

    def test_a_correct_unequal_split_is_preserved(self):
        request, report = normalize(self.facts((0.2, 0.3, 0.5)),
                                    SCENARIOS['toluene']['text'])
        stoich = request.reactions[0].stoichiometry
        self.assertEqual([stoich[n] for n in ('o-Xylene', 'm-Xylene', 'p-Xylene')],
                         [0.2, 0.3, 0.5])
        self.assertFalse(any('group total restored' in note for note in report.applied))

    def test_no_written_equation_means_no_coefficient_repair(self):
        request, _ = normalize(self.facts(), '甲苯歧化，生成苯和邻、间、对二甲苯')
        self.assertEqual(request.reactions[0].stoichiometry['o-Xylene'], 1)

    def test_another_wrong_coefficient_is_not_hidden_by_the_repair(self):
        facts = self.facts()
        facts['reactions'][0]['species'][0]['coefficient'] = -3
        request, _ = normalize(facts, SCENARIOS['toluene']['text'])
        self.assertEqual(request.reactions[0].stoichiometry['o-Xylene'], 1)
        state = initial_state(SCENARIOS['toluene']['text'], kind='conversion',
                              feed_basis='mass_fraction')
        state['facts'] = facts
        result = make_plan_node()(state)
        self.assertEqual(result['status'], 'WAITING_INPUT')
        self.assertEqual(result['cases'], [])

    def test_ambiguous_written_equations_do_not_authorize_a_repair(self):
        text = '2C7H8 -> C6H6 + C8H10; 2C7H8 -> C6H6 + C8H10'
        request, _ = normalize(self.facts(), text)
        self.assertEqual(request.reactions[0].stoichiometry['o-Xylene'], 1)


if __name__ == '__main__':
    unittest.main()
