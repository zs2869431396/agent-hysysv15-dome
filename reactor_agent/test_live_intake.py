"""Offline replay of real replies: shape alone must not imply a correct feed."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from reactor_agent.__main__ import SCENARIOS, defaults_answerer
from reactor_agent.extraction import SYSTEM_PROMPT, allowed_keys, validate_facts
from reactor_agent.nodes.answers import apply_answers
from reactor_agent.nodes.plan import make_plan_node
from reactor_agent.nodes.state import initial_state
from reactor_agent.normalize import normalize
from reactor_agent.test_normalize import GASIFICATION_FACTS

FIXTURES = Path(__file__).with_name('fixtures') / 'live_intake'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8'))


def plan(facts, answers=None):
    scenario = SCENARIOS['gasification']
    state = initial_state(scenario['text'], scenario_label='gasification',
                          kind=scenario['kind'], phase=scenario['phase'],
                          feed_basis=scenario['feed_basis'])
    state['facts'] = facts
    state['answers'] = answers or {}
    return make_plan_node()(state)


class RealReplyRegression(unittest.TestCase):
    def test_all_recorded_replies_have_the_exact_contract_and_pass_schema(self):
        for name in ('toluene', 'smr', 'gasification_mixture', 'gasification_inverted',
                     'gasification_missing_water_qwen'):
            with self.subTest(reply=name):
                facts = fixture(name)
                self.assertEqual(set(facts), set(allowed_keys()))
                self.assertEqual(validate_facts(facts), [])

    def test_unreadable_mixture_is_one_question_with_a_grounded_default(self):
        result = plan(fixture('gasification_mixture'))
        self.assertEqual(result['status'], 'WAITING_INPUT')
        self.assertEqual({q['id'] for q in result['blocking']},
                         {'q-feed-composition', 'q-volumetric-flow', 'q-coal-definition'})
        q = next(q for q in result['blocking'] if q['id'] == 'q-feed-composition')
        self.assertEqual(json.loads(q['default'])['composition'],
                         [{'name': '煤炭', 'fraction': 62}, {'name': '水', 'fraction': 38}])

    def test_inverted_real_reply_cannot_reach_ready_even_with_coal_and_flow_confirmed(self):
        result = plan(fixture('gasification_inverted'),
                      {'q-coal-definition': '按纯碳处理', 'q-volumetric-flow': '默认'})
        self.assertEqual(result['status'], 'WAITING_INPUT')
        self.assertEqual([q['id'] for q in result['blocking']], ['q-feed-composition'])
        self.assertEqual(result['cases'], [])

    def test_qwen_missing_water_cannot_be_normalized_to_pure_carbon(self):
        facts = fixture('gasification_missing_water_qwen')
        self.assertEqual(len(facts['feed_composition']), 1)
        result = plan(facts, {'q-coal-definition': '按纯碳处理',
                              'q-volumetric-flow': '默认'})
        self.assertEqual(result['status'], 'WAITING_INPUT')
        self.assertEqual([q['id'] for q in result['blocking']], ['q-feed-composition'])
        self.assertEqual(result['cases'], [])

    def test_accepting_grounded_defaults_corrects_both_bad_replies_without_a_model_call(self):
        for name in ('gasification_mixture', 'gasification_inverted',
                     'gasification_missing_water_qwen'):
            with self.subTest(reply=name):
                facts = fixture(name)
                paused = plan(facts)
                answers = defaults_answerer(paused['blocking'])
                result = plan(facts, answers)
                self.assertEqual(result['status'], 'READY')
                self.assertEqual(result['blocking'], [])
                feed = result['cases'][0]['spec']['feeds']
                feed = feed[0] if isinstance(feed, list) else feed
                self.assertEqual(feed['fractions'], {'Carbon': 0.62, 'Water': 0.38})
                self.assertEqual(feed['basis'], 'mass_fraction')
                self.assertEqual(result['cases'][0]['spec']['reactor']['solid_carbon'],
                                 'saturation')

    def test_valid_slurry_has_only_the_two_independent_confirmation_questions(self):
        result = plan(GASIFICATION_FACTS)
        self.assertEqual({q['id'] for q in result['blocking']},
                         {'q-volumetric-flow', 'q-coal-definition'})

    def test_composition_basis_is_confirmed_together_with_the_percentages(self):
        facts = copy.deepcopy(GASIFICATION_FACTS)
        facts['composition_basis'] = 'molar_fraction'
        paused = plan(facts)
        self.assertIn('q-feed-composition', [q['id'] for q in paused['blocking']])
        result = plan(facts, defaults_answerer(paused['blocking']))
        self.assertEqual(result['status'], 'READY')

    def test_unknown_components_do_not_compile_as_a_pure_surviving_component(self):
        facts = copy.deepcopy(GASIFICATION_FACTS)
        facts['feed_composition'] = [{'name': '水', 'fraction': 38},
                                     {'name': '未知固体', 'fraction': 62},
                                     {'name': '未知液体', 'fraction': 1}]
        request, report = normalize(facts, '未知固体、未知液体和水的进料')
        composition = [q for q in report.questions if q.field == 'feeds[0].fractions']
        self.assertEqual(len(composition), 1)
        self.assertEqual(composition[0].id, 'q-feed-composition')
        self.assertIsNone(composition[0].default)
        self.assertEqual(request.feeds[0].fractions, {})

    def test_missing_composition_has_no_invented_default(self):
        facts = copy.deepcopy(GASIFICATION_FACTS)
        facts['feed_composition'] = []
        _, report = normalize(facts, '煤炭和水，未说明比例')
        questions = [q for q in report.questions if q.field == 'feeds[0].fractions']
        self.assertEqual(len(questions), 1)
        self.assertIsNone(questions[0].default)

    def test_ambiguous_slurry_has_no_source_derived_default(self):
        facts = fixture('gasification_mixture')
        for text in ('水煤浆进料浓度62wt%，添加助剂',
                     '水煤浆浓度62wt%，另有水煤浆浓度50wt%',
                     '水煤浆浓度120wt%'):
            with self.subTest(text=text):
                _, report = normalize(facts, text)
                q = next(q for q in report.questions if q.id == 'q-feed-composition')
                self.assertIsNone(q.default)

    def test_old_paused_species_question_accepts_a_complete_replacement(self):
        answer = {'composition': [{'name': '煤炭', 'fraction': 62},
                                  {'name': '水', 'fraction': 38}],
                  'basis': 'mass_fraction'}
        facts = apply_answers(fixture('gasification_mixture'),
                              {'q-composition-species-b07fc63d': json.dumps(answer)})
        self.assertEqual(facts['feed_composition'], answer['composition'])
        self.assertEqual(facts['composition_basis'], 'mass_fraction')

    def test_prompt_requires_separate_feed_substances_and_uses_an_unrelated_reaction(self):
        self.assertIn('List each feed substance separately', SYSTEM_PROMPT)
        self.assertIn('coal mass percentage', SYSTEM_PROMPT)
        self.assertIn('For ethanol -> ethylene + water', SYSTEM_PROMPT)
        self.assertNotIn('For 2C7H8', SYSTEM_PROMPT)


if __name__ == '__main__':
    unittest.main()
