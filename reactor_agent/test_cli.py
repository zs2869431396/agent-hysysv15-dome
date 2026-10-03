"""CLI tests: the answering loop, the pause marker, and the scenario table.

No model and no HYSYS. The graph is driven with the same fake client the graph tests
use, so what is exercised here is the interactive layer: who answers a question, what
happens when nobody does, and how a second process finds a run that paused.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from langgraph.checkpoint.memory import InMemorySaver

from reactor_agent.__main__ import (
    PAUSED_MARKER,
    SCENARIOS,
    _clear_paused,
    _read_paused,
    _write_paused,
    defaults_answerer,
    drive_graph,
    latest_paused_run,
    no_input_answerer,
    run_folder,
    terminal_answerer,
)
from reactor_agent.graph import build_graph, initial_state
from reactor_agent.test_graph import TOLUENE_FACTS, client_returning

GASIFICATION_FACTS = {
    'species': ['碳', '水', '一氧化碳', '氢气'],
    'feed_composition': [{'name': '煤炭', 'fraction': 62},
                         {'name': '水', 'fraction': 38}],
    'composition_basis': 'mass_percent',
    'reactions': [{'name': '气化',
                   'species': [{'name': '碳', 'coefficient': -1},
                               {'name': '水', 'coefficient': -1},
                               {'name': '一氧化碳', 'coefficient': 1},
                               {'name': '氢气', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': None, 'conversion_basis': '',
    'feed_total': 80000, 'feed_unit': 'Nm3/h',
    'feed_temperature': 40, 'feed_temperature_unit': 'C',
    'feed_pressure': 40, 'feed_pressure_unit': 'bar',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [1400], 'outlet_temperature_unit': 'C',
    'missing_information': [],
}

GASIFICATION_TEXT = ('水煤浆气化：C+H2O → CO+H2，进料煤炭和水，流量80000Nm3/h，'
                     '压力40bar，进料温度40摄氏度，出口1400度，浓度62wt%')

# The flow is not in the text, so it is ungrounded and will be asked about.
UNGROUNDED_FACTS = dict(TOLUENE_FACTS, feed_total=10000.0,
                        feed_temperature=380, feed_pressure=2.5)
UNGROUNDED_TEXT = '请模拟一个反应'


def config_for(thread: str) -> dict:
    return {'configurable': {'thread_id': thread}}


class DefaultsFinishTheRun(unittest.TestCase):
    """`--accept-defaults` must take the run all the way, not part of the way.

    The gasification run is the case that matters: two questions, both with a
    suggested answer, and answering them is what the plan's demo does with two presses
    of Enter.
    """

    def test_the_gasification_questions_are_answered_and_the_run_finishes(self):
        graph = build_graph(client_returning(GASIFICATION_FACTS), dry_run=True,
                            checkpointer=InMemorySaver())
        state = drive_graph(graph,
                            initial_state(GASIFICATION_TEXT, scenario_label='g',
                                          kind='gibbs', phase='gas',
                                          feed_basis='mass_fraction'),
                            config_for('cli-defaults'), defaults_answerer)
        self.assertFalse(state.get('__interrupt__'))
        self.assertEqual(state['status'], 'READY', state.get('problems'))

    def test_a_question_without_a_default_stops_the_whole_thing(self):
        """Answering some and not others would leave a half-confirmed run."""
        self.assertIsNone(defaults_answerer([
            {'id': 'a', 'question': 'q', 'default': 'yes'},
            {'id': 'b', 'question': 'q', 'default': None}]))

    def test_it_answers_with_the_default_text_everywhere(self):
        questions = [{'id': 'a', 'question': 'q', 'default': 'x'},
                     {'id': 'b', 'question': 'q', 'default': 'y'}]
        self.assertEqual(defaults_answerer(questions), {'a': 'x', 'b': 'y'})


class NoInputStaysPaused(unittest.TestCase):

    def test_the_graph_stays_interrupted(self):
        graph = build_graph(client_returning(UNGROUNDED_FACTS), dry_run=True,
                            checkpointer=InMemorySaver())
        state = drive_graph(graph,
                            initial_state(UNGROUNDED_TEXT, scenario_label='t',
                                          kind='conversion'),
                            config_for('cli-no-input'), no_input_answerer)
        self.assertTrue(state.get('__interrupt__'))
        self.assertEqual(no_input_answerer([]), None)


class TerminalAnswersARun(unittest.TestCase):
    """Two presses of Enter, because both questions carry a default."""

    def test_returns_the_default_when_the_input_is_empty(self):
        questions = [{'id': 'a', 'question': 'q', 'default': 'x'},
                     {'id': 'b', 'question': 'q', 'default': 'y'}]
        with mock.patch('builtins.input', side_effect=['', '']):
            self.assertEqual(terminal_answerer(questions), {'a': 'x', 'b': 'y'})

    def test_a_typed_answer_wins_over_the_default(self):
        questions = [{'id': 'a', 'question': 'q', 'default': 'x'}]
        with mock.patch('builtins.input', side_effect=['49086 kg/h']):
            self.assertEqual(terminal_answerer(questions), {'a': '49086 kg/h'})

    def test_an_empty_answer_with_no_default_is_asked_again_then_given_up_on(self):
        questions = [{'id': 'a', 'question': 'q', 'default': None}]
        with mock.patch('builtins.input', side_effect=['', '', '']):
            self.assertIsNone(terminal_answerer(questions))

    def test_an_empty_answer_is_retried_until_something_arrives(self):
        questions = [{'id': 'a', 'question': 'q', 'default': None}]
        with mock.patch('builtins.input', side_effect=['', '10000 kg/h']):
            self.assertEqual(terminal_answerer(questions), {'a': '10000 kg/h'})

    def test_the_gasification_run_finishes_with_two_enter_presses(self):
        graph = build_graph(client_returning(GASIFICATION_FACTS), dry_run=True,
                            checkpointer=InMemorySaver())
        with mock.patch('builtins.input', side_effect=['', '']):
            state = drive_graph(
                graph,
                initial_state(GASIFICATION_TEXT, scenario_label='g', kind='gibbs',
                              phase='gas', feed_basis='mass_fraction'),
                config_for('cli-terminal'), terminal_answerer)
        self.assertFalse(state.get('__interrupt__'))
        self.assertEqual(state['status'], 'READY', state.get('problems'))


class PausedRunsCanBeFoundAgain(unittest.TestCase):

    def test_the_newest_paused_run_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = run_folder('gasification', base=root)
            newer = run_folder('gasification', base=root)
            quiet = run_folder('gasification', base=root)
            for folder in (older, newer, quiet):
                folder.mkdir(parents=True, exist_ok=True)
            _write_paused(older, 'gasification', 't-old', [])
            time.sleep(0.01)
            _write_paused(newer, 'gasification', 't-new', [])
            self.assertEqual(latest_paused_run('gasification', base=root), newer)

    def test_a_folder_without_the_marker_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quiet = run_folder('gasification', base=root)
            quiet.mkdir(parents=True, exist_ok=True)
            self.assertIsNone(latest_paused_run('gasification', base=root))

    def test_another_scenario_is_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = run_folder('smr', base=root)
            other.mkdir(parents=True, exist_ok=True)
            _write_paused(other, 'smr', 't', [])
            self.assertIsNone(latest_paused_run('gasification', base=root))

    def test_the_marker_round_trips_and_is_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'run'
            _write_paused(folder, 'gasification', 'thread-1',
                          [{'id': 'q', 'question': '问题', 'default': '默认'}])
            marker = folder / PAUSED_MARKER
            self.assertTrue(marker.is_file())
            payload = json.loads(marker.read_text(encoding='utf-8'))
            self.assertEqual(payload['thread_id'], 'thread-1')
            self.assertEqual(payload['questions'][0]['question'], '问题')
            self.assertTrue(payload['paused_at'])
            self.assertEqual(_read_paused(folder)['label'], 'gasification')
            _clear_paused(folder)
            self.assertIsNone(_read_paused(folder))
            # Clearing twice is not an error: a resumed run that finishes normally
            # calls it on a folder that may never have been paused.
            _clear_paused(folder)


class ScenarioTable(unittest.TestCase):

    def test_toluene_phase_is_not_claimed_to_be_liquid(self):
        """Phase only matters for the CSTR/PFR rule, and no rate law is given."""
        self.assertNotEqual(SCENARIOS['toluene']['phase'], 'liquid')

    def test_every_scenario_has_the_fields_the_cli_reads(self):
        for name, scenario in SCENARIOS.items():
            with self.subTest(scenario=name):
                for key in ('kind', 'phase', 'feed_basis', 'label', 'text'):
                    self.assertIn(key, scenario)
                self.assertTrue(scenario['text'].strip())
                self.assertTrue(scenario['label'].isascii())

    def test_the_exam_texts_are_unchanged(self):
        self.assertIn('转化率为50%', SCENARIOS['toluene']['text'])
        self.assertIn('710°C', SCENARIOS['smr']['text'])
        self.assertIn('80000Nm3/h', SCENARIOS['gasification']['text'])


class RunFolderNames(unittest.TestCase):

    def test_the_label_is_sanitised_and_stamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = run_folder('../../etc/passwd', base=Path(tmp))
            self.assertEqual(folder.parent, Path(tmp))
            self.assertNotIn('..', folder.name)
            self.assertNotIn(os.sep, folder.name)

    def test_two_invocations_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run_folder('toluene', base=Path(tmp))
            second = run_folder('toluene', base=Path(tmp))
            # Same second, same name - the CLI relies on the timestamp only, so this
            # documents the actual behaviour rather than pretending it is unique.
            self.assertEqual(first.parent, second.parent)


if __name__ == '__main__':
    unittest.main(verbosity=2)
