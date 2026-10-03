"""Tests for the main graph: routing, pausing, and resuming.

The behaviour that matters most is what does NOT happen while a run is paused. A
question that is outstanding must mean zero cases opened, zero files in the run
folder and zero HYSYS calls - so that is asserted directly, not inferred.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from reactor_agent.adapters.hysys_cli import ExecutionResult, HysysCliAdapter
from reactor_agent.adapters.run_store import RunStore
from reactor_agent.graph import (
    build_graph,
    initial_state,
    state_summary,
)
from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.pipeline import PASS, WAITING_INPUT

TOLUENE_FACTS = {
    'species': ['甲苯', '苯', '邻二甲苯'],
    'feed_composition': [{'name': '甲苯', 'fraction': 1.0}],
    'composition_basis': 'pure',
    'reactions': [{'name': '歧化',
                   'species': [{'name': '甲苯', 'coefficient': -2},
                   {'name': '苯', 'coefficient': 1},
                                {'name': '邻二甲苯', 'coefficient': 1}],
                   'reversible': False}],
    'conversion_percent': 50, 'conversion_basis': '甲苯',
    'feed_total': 10000, 'feed_unit': 'kg/h',
    'feed_temperature': 380, 'feed_temperature_unit': '℃',
    'feed_pressure': 2.5, 'feed_pressure_unit': 'MPa',
    'case_pressures': [], 'case_pressure_unit': '',
    'outlet_temperatures': [], 'outlet_temperature_unit': '',
    'missing_information': [],
}

# Missing the flow, so the run must stop and ask for it.
INCOMPLETE_FACTS = dict(TOLUENE_FACTS, feed_total=None, feed_unit='')

# The equation belongs to the request; see the note in test_pipeline.py.
TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料流量10000kg/h，进料温度380℃，压力2.5MPa，转化率50%')


def client_returning(*fact_sets: dict) -> ChatClient:
    """A client that returns each fact set in turn, then repeats the last.

    Note a subtlety this caused once: `extract_verified` retries when a required
    field is empty, so a queue of (incomplete, complete) silently produces a complete
    extraction on the retry and never asks anything. Pass ONE set to make the
    incompleteness stick.
    """
    queue = []
    for facts in fact_sets:
        queue.append((200, json.dumps({
            'choices': [{'finish_reason': 'stop',
                         'message': {'content': json.dumps(facts)}}],
            'usage': {}})))
    config = LlmConfig(base='https://example.test/v1', key='sk-' + 'g' * 30,
                       min_interval=0)

    def transport(url, payload, headers, timeout):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return ChatClient(config, transport=transport, sleeper=lambda _s: None)


class RecordingAdapter(HysysCliAdapter):
    def __init__(self, statuses=None):
        super().__init__(Path(tempfile.gettempdir()), python='python', use_lock=False)
        self.calls: list[str] = []
        self.statuses = list(statuses or [])

    def run_case(self, spec, case_id, run_root, attempt=1):
        self.calls.append(case_id)
        status = self.statuses.pop(0) if self.statuses else 'PASS'
        # A PASS carries evidence, because the adapter now insists on it: a result
        # that says PASS with no case file proves nothing was simulated.
        payload = {'status': status, 'case_file': 'x.hsc'}
        if status != 'PASS':
            payload['error_type'] = 'result_check'
            payload['error'] = 'simulated failure'
        return ExecutionResult(case_id=case_id, attempt=attempt,
                               run_dir=Path(run_root) / case_id, status=status,
                               result=payload, seconds=0.5)


def config_for(thread: str) -> dict:
    return {'configurable': {'thread_id': thread}}


class StraightThrough(unittest.TestCase):

    def test_a_complete_request_reaches_a_ready_or_executed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), run_root=Path(tmp),
                                dry_run=True, checkpointer=InMemorySaver())
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('t1'))
        self.assertEqual(state['status'], 'READY')
        self.assertEqual(state['executions'], [])
        self.assertIn('conversion', state['decision']['executed'])

    def test_the_explanation_is_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), run_root=Path(tmp),
                                dry_run=True)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion'),
                                 config_for('t2'))
        self.assertTrue(state['explanation'])
        self.assertIn('选型', state['explanation'])

    def test_execution_runs_when_not_a_dry_run(self):
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=Path(tmp), dry_run=False)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('t3'))
        self.assertEqual(state['status'], 'PASS')
        self.assertEqual(adapter.calls, ['single'])


class PausingToAsk(unittest.TestCase):
    """The promise: a paused run has touched nothing."""

    def test_an_incomplete_request_pauses_instead_of_guessing(self):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = build_graph(client_returning(INCOMPLETE_FACTS), adapter=adapter,
                                run_root=root, dry_run=False)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('p1'))
            # Nothing was simulated...
            self.assertEqual(adapter.calls, [])
            # ...and nothing was written into the run folder either.
            self.assertFalse((root / 'ledger.jsonl').exists())

    def test_the_interrupt_carries_the_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(INCOMPLETE_FACTS),
                                run_root=Path(tmp), dry_run=False)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('p2'))
        interrupts = state.get('__interrupt__')
        self.assertTrue(interrupts, 'expected the graph to pause')
        payload = interrupts[0].value
        self.assertEqual(payload['kind'], 'clarification')
        self.assertTrue(payload['questions'])

    def test_answering_resumes_and_completes(self):
        """A resume supplies the missing flow, and the run then proceeds."""
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            # One fact set only, so the retry cannot quietly fill the gap.
            graph = build_graph(client_returning(INCOMPLETE_FACTS),
                                adapter=adapter, run_root=Path(tmp), dry_run=False)
            config = config_for('p3')
            paused = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                                kind='conversion', phase='liquid',
                                                feed_basis='mass_fraction'), config)
            self.assertIn('__interrupt__', paused)
            self.assertEqual(adapter.calls, [])
            from langgraph.types import Command
            state = graph.invoke(
                Command(resume={'q-feed-flow': {'value': 10000, 'unit': 'kg/h'}}),
                config)
        self.assertNotIn('__interrupt__', state)
        self.assertIn(state['status'], ('READY', 'PASS'))

    def test_the_answer_reaches_the_spec(self):
        """The resumed run must compile with the flow the user supplied."""
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(INCOMPLETE_FACTS),
                                adapter=adapter, run_root=Path(tmp), dry_run=False)
            config = config_for('p5')
            graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                       kind='conversion', phase='liquid',
                                       feed_basis='mass_fraction'), config)
            from langgraph.types import Command
            state = graph.invoke(
                Command(resume={'q-feed-flow': {'value': 10000, 'unit': 'kg/h'}}),
                config)
        spec = (state.get('cases') or [{}])[0].get('spec') or {}
        self.assertEqual(spec['feeds'][0]['total_flow'], 10000)
        self.assertEqual(spec['feeds'][0]['total_flow_unit'], 'kg/h')

    def test_an_answer_does_not_get_asked_for_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(INCOMPLETE_FACTS),
                                run_root=Path(tmp), dry_run=True)
            config = config_for('p4')
            graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                       kind='conversion', phase='liquid',
                                       feed_basis='mass_fraction'), config)
            from langgraph.types import Command
            state = graph.invoke(Command(resume={'q-feed-flow': 10000}), config)
        self.assertNotIn('__interrupt__', state)


class RoutingRules(unittest.TestCase):

    def test_a_dry_run_never_reaches_execution(self):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=Path(tmp), dry_run=True)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion'),
                                 config_for('r1'))
        self.assertEqual(adapter.calls, [])
        self.assertEqual(state['status'], 'READY')

    def test_a_failed_case_is_reflected_in_the_status(self):
        adapter = RecordingAdapter(statuses=['FAILED'])
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=Path(tmp), dry_run=False)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('r2'))
        self.assertEqual(state['status'], 'FAILED')
        self.assertTrue(state['problems'])

    def test_an_unreachable_workstation_stops_further_cases(self):
        adapter = RecordingAdapter(statuses=['CANNOT_CONNECT_TO_HYSYS', 'PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=Path(tmp), dry_run=False)
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('r3'))
        self.assertEqual(adapter.calls, ['single'])
        self.assertTrue(any('did not answer' in p for p in state['problems']))


class AnswerParsing(unittest.TestCase):
    """Answers arrive as free text, so they must be read leniently - and a value
    that cannot be read must never reach the contract and surface as a traceback."""

    def test_a_bare_number(self):
        from reactor_agent.graph import parse_answer_value
        self.assertEqual(parse_answer_value(10000), (10000.0, None))
        self.assertEqual(parse_answer_value('10000'), (10000.0, None))

    def test_a_number_with_a_unit(self):
        from reactor_agent.graph import parse_answer_value
        self.assertEqual(parse_answer_value('10000 kg/h'), (10000.0, 'kg/h'))
        self.assertEqual(parse_answer_value('380 C'), (380.0, 'C'))

    def test_a_structured_answer(self):
        from reactor_agent.graph import parse_answer_value
        self.assertEqual(parse_answer_value({'value': 10000, 'unit': 'kg/h'}),
                         (10000.0, 'kg/h'))
        self.assertEqual(parse_answer_value({'value': '10000 kg/h'}),
                         (10000.0, 'kg/h'))

    def test_something_unusable_is_reported_not_crashed(self):
        from reactor_agent.graph import parse_answer_value
        self.assertEqual(parse_answer_value('about ten thousand'), (None, None))
        self.assertEqual(parse_answer_value(None), (None, None))
        self.assertEqual(parse_answer_value(True), (None, None))

    def test_an_unreadable_answer_leaves_the_fact_alone_and_says_so(self):
        from reactor_agent.graph import _apply_answers
        facts = {'feed_total': None}
        notes: list[str] = []
        merged = _apply_answers(facts, {'q-feed-flow': 'lots'}, note=notes)
        self.assertIsNone(merged['feed_total'])
        self.assertTrue(notes)

    def test_the_flow_unit_lands_in_feed_unit_not_feed_total_unit(self):
        from reactor_agent.graph import _apply_answers
        merged = _apply_answers({}, {'q-ungrounded:feed_total': '10000 kg/h'})
        self.assertEqual(merged['feed_total'], 10000.0)
        self.assertEqual(merged['feed_unit'], 'kg/h')
        self.assertNotIn('feed_total_unit', merged)

    def test_an_unknown_question_id_writes_nothing(self):
        """A client must not reach an arbitrary field by inventing an id."""
        from reactor_agent.graph import _apply_answers
        merged = _apply_answers({'feed_total': None},
                                {'q-made-up': 123, 'components': 'evil'})
        self.assertEqual(merged, {'feed_total': None})


class ResumeDoesNotRedoWork(unittest.TestCase):

    def test_a_second_invocation_on_the_same_thread_uses_the_checkpoint(self):
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=Path(tmp), dry_run=False)
            config = config_for('c1')
            state = initial_state(TEXT, scenario_label='toluene', kind='conversion',
                                  phase='liquid', feed_basis='mass_fraction')
            first = graph.invoke(state, config)
            self.assertEqual(first['status'], 'PASS')
            # Re-invoking the same thread returns the finished state without rerunning.
            again = graph.invoke({}, config)
        self.assertEqual(again['status'], 'PASS')

    def test_the_ledger_prevents_a_repeat_even_on_a_new_thread(self):
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = build_graph(client_returning(TOLUENE_FACTS), adapter=adapter,
                                run_root=root, dry_run=False)
            state = initial_state(TEXT, scenario_label='toluene', kind='conversion',
                                  phase='liquid', feed_basis='mass_fraction')
            graph.invoke(state, config_for('c2'))
            self.assertEqual(len(adapter.calls), 1)
            second = RecordingAdapter()
            graph2 = build_graph(client_returning(TOLUENE_FACTS), adapter=second,
                                 run_root=root, dry_run=False)
            resumed = graph2.invoke(state, config_for('c3'))
        self.assertEqual(second.calls, [])
        self.assertTrue(any('already has a result' in p for p in resumed['problems']))


class Checkpointing(unittest.TestCase):

    def test_state_survives_a_new_graph_object(self):
        """A SQLite checkpointer is what makes this work across process exits."""
        from reactor_agent.graph import open_checkpointer
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'checkpoints.sqlite'
            config = config_for('s1')
            with open_checkpointer(db) as saver:
                graph = build_graph(client_returning(TOLUENE_FACTS), dry_run=True,
                                    checkpointer=saver)
                graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                           kind='conversion'), config)
            self.assertTrue(db.is_file())


class UnreadableAnswerDoesNotReleaseTheValue(unittest.TestCase):
    """The defect this class exists for.

    A fabricated flow was correctly blocked. The user then replied "not a number",
    and `ask_node` cleared the whole ungrounded list anyway - so the fabricated value
    was handed to the adapter while having *appeared* to be confirmed. Blocking and
    then releasing on an unreadable answer is worse than never blocking.

    Every test here fails against that version.
    """

    FABRICATED = dict(TOLUENE_FACTS, feed_total=12345.0, feed_unit='kg/h')
    NO_FLOW_TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料温度380℃，压力2.5MPa，转化率50%')

    def _paused(self, adapter, root):
        graph = build_graph(client_returning(self.FABRICATED), adapter=adapter,
                            run_root=root, dry_run=False)
        config = config_for('u')
        state = graph.invoke(initial_state(self.NO_FLOW_TEXT, scenario_label='t',
                                           kind='conversion', phase='liquid',
                                           feed_basis='mass_fraction'), config)
        return graph, config, state

    def test_an_unreadable_answer_does_not_start_the_run(self):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            graph, config, first = self._paused(adapter, Path(tmp))
            self.assertTrue(first.get('__interrupt__'))
            self.assertEqual(adapter.calls, [])
            from langgraph.types import Command
            after = graph.invoke(
                Command(resume={'q-ungrounded:feed_total': 'not a number'}), config)
        self.assertEqual(after['status'], WAITING_INPUT)
        self.assertEqual(adapter.calls, [])

    def test_the_field_stays_ungrounded(self):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            graph, config, _ = self._paused(adapter, Path(tmp))
            from langgraph.types import Command
            after = graph.invoke(
                Command(resume={'q-ungrounded:feed_total': 'not a number'}), config)
        self.assertIn('feed_total', after['ungrounded'])

    def test_the_user_is_asked_again(self):
        adapter = RecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            graph, config, _ = self._paused(adapter, Path(tmp))
            from langgraph.types import Command
            after = graph.invoke(
                Command(resume={'q-ungrounded:feed_total': 'not a number'}), config)
        self.assertTrue(after.get('__interrupt__'), 'expected another question')
        questions = after['__interrupt__'][0].value['questions']
        self.assertTrue(any(q['field'] == 'feed_total' for q in questions))

    def test_a_readable_answer_releases_it_and_uses_the_users_value(self):
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            graph, config, _ = self._paused(adapter, Path(tmp))
            from langgraph.types import Command
            after = graph.invoke(
                Command(resume={'q-ungrounded:feed_total': '10000 kg/h'}), config)
        self.assertEqual(after['status'], PASS)
        spec = (after.get('cases') or [{}])[0].get('spec') or {}
        # The user's figure, not the fabricated 12345.
        self.assertEqual(spec['feeds'][0]['total_flow'], 10000.0)
        self.assertEqual(spec['feeds'][0]['total_flow_unit'], 'kg/h')

    def test_an_unanswered_second_field_keeps_blocking(self):
        """Only what was answered is released; the rest still blocks."""
        from reactor_agent.nodes.answers import resolve_ungrounded
        resolved, unresolved, _ = resolve_ungrounded(
            ['feed_total', 'feed_pressure'],
            {'q-ungrounded:feed_total': '10000 kg/h'})
        self.assertEqual(resolved, ['feed_total'])
        self.assertEqual(unresolved, ['feed_pressure'])

    def test_an_answer_that_parses_as_text_is_not_accepted(self):
        from reactor_agent.nodes.answers import resolve_ungrounded
        for bad in ('about ten thousand', '', 'lots', None, True):
            with self.subTest(bad=bad):
                resolved, unresolved, notes = resolve_ungrounded(
                    ['feed_total'], {'q-ungrounded:feed_total': bad})
                self.assertEqual(resolved, [])
                self.assertEqual(unresolved, ['feed_total'])

    def test_the_pause_loop_is_bounded(self):
        """A caller that keeps replying unreadably must not loop forever."""
        from reactor_agent.nodes.ask import MAX_CLARIFICATION_ROUNDS, route_after_plan
        state = {'blocking': [{'id': 'x'}],
                 'clarification_rounds': MAX_CLARIFICATION_ROUNDS}
        self.assertEqual(route_after_plan(state), 'explain')
        state['clarification_rounds'] = 0
        self.assertEqual(route_after_plan(state), 'ask')

    def test_there_is_no_stop_after_one_answer(self):
        """The old rule dropped a second, still-open question silently."""
        from reactor_agent.nodes.ask import route_after_plan
        state = {'blocking': [{'id': 'x'}], 'answers': {'a': 1},
                 'clarification_rounds': 1}
        self.assertEqual(route_after_plan(state), 'ask')


class CompilerQuestionsReachTheUser(unittest.TestCase):
    """The defect: the run stopped with an empty question list and asked nothing.

    Normalisation's questions were placed in the state, then the compiler added its
    own (`feed_questions`, `coal_questions`) and returned WAITING_INPUT. Those extra
    questions were never read back, so `blocking` stayed empty, routing went straight
    to `explain`, and the user was told nothing at all - a run that stops silently is
    indistinguishable from a run that finished.
    """

    GASIFICATION = {
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
        'feed_total': 80000, 'feed_unit': 'kg/h',
        'feed_temperature': 40, 'feed_temperature_unit': 'C',
        'feed_pressure': 40, 'feed_pressure_unit': 'bar',
        'case_pressures': [], 'case_pressure_unit': '',
        'outlet_temperatures': [1400], 'outlet_temperature_unit': 'C',
        'missing_information': [],
    }

    TEXT = ('水煤浆气化：C+H2O → CO+H2，进料煤炭和水，流量80000kg/h，压力40bar，'
            '进料温度40摄氏度，出口1400度，浓度62wt%')

    def _run(self, **resume):
        from langgraph.types import Command
        graph = build_graph(client_returning(self.GASIFICATION), dry_run=True)
        config = config_for('coal')
        state = initial_state(self.TEXT, scenario_label='g', kind='gibbs',
                              phase='gas', feed_basis='mass_fraction')
        first = graph.invoke(state, config)
        if resume:
            return graph.invoke(Command(resume=resume), config)
        return first

    def test_the_compiler_question_is_reported(self):
        state = self._run()
        ids = [q['id'] for q in (state.get('blocking') or [])]
        self.assertIn('q-coal-definition', ids)

    def test_the_run_actually_asks(self):
        state = self._run()
        self.assertTrue(state.get('__interrupt__'), 'expected a question')
        questions = state['__interrupt__'][0].value['questions']
        self.assertTrue(any(q['id'] == 'q-coal-definition' for q in questions))

    def test_a_confirmation_answers_it(self):
        from reactor_agent.nodes.answers import confirmation_text
        text = confirmation_text({'q-coal-definition': '可以'}, self.TEXT)
        self.assertIn('纯固体碳', text)

    def test_a_non_answer_does_not(self):
        from reactor_agent.nodes.answers import confirmation_text
        for bad in ('不知道', 'no idea', ''):
            with self.subTest(bad=bad):
                self.assertEqual(confirmation_text({'q-coal-definition': bad},
                                                   self.TEXT), self.TEXT)

    def test_a_second_open_question_is_still_asked(self):
        """The old rule - stop after one answer - dropped it silently."""
        from reactor_agent.nodes.ask import route_after_plan
        state = {'blocking': [{'id': 'q-coal-definition'}],
                 'answers': {'q-feed-flow': 1}, 'clarification_rounds': 1}
        self.assertEqual(route_after_plan(state), 'ask')


class CompileFailureIsReportedAsFailure(unittest.TestCase):
    """A failed compilation used to look like a run waiting for input.

    `compiled = plan` left the status at its default WAITING_INPUT while the problem
    list held the compile error and the question list was empty: the user was told to
    answer questions that did not exist.
    """

    def test_a_compile_error_ends_as_failed(self):
        from unittest import mock

        from reactor_agent.compiler import CompileError
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), dry_run=True,
                                run_root=Path(tmp))
            with mock.patch('reactor_agent.nodes.plan.compile_plan',
                            side_effect=CompileError('boom')):
                state = graph.invoke(
                    initial_state(TEXT, scenario_label='toluene', kind='conversion',
                                  phase='liquid', feed_basis='mass_fraction'),
                    config_for('boom'))
        self.assertEqual(state['status'], 'FAILED')
        self.assertTrue(any('compilation failed' in p for p in state['problems']),
                        state['problems'])
        self.assertFalse(state.get('__interrupt__'))


class InterruptCarriesTheDefaultAnswer(unittest.TestCase):
    """The suggested answer has to reach whoever is asking the question."""

    def test_the_question_dictionary_has_a_default_key(self):
        graph = build_graph(client_returning(INCOMPLETE_FACTS), dry_run=True)
        state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                           kind='conversion', phase='liquid',
                                           feed_basis='mass_fraction'),
                             config_for('default-key'))
        questions = state['__interrupt__'][0].value['questions']
        self.assertTrue(questions)
        for question in questions:
            self.assertIn('default', question)


class NormalVolumeAnswers(unittest.TestCase):
    """The Nm3 question, its default, and the one-round route out of it.

    This is the regression test for the loop the plan describes: normalisation asked
    about the feed's total flow, the compiler asked again about the same field under a
    different id, and only the first id was routable - so answering never satisfied the
    compiler's question and the run asked forever.
    """

    GASIFICATION = dict(CompilerQuestionsReachTheUser.GASIFICATION,
                        feed_unit='Nm3/h')

    TEXT = ('水煤浆气化：C+H2O → CO+H2，进料煤炭和水，流量80000Nm3/h，压力40bar，'
            '进料温度40摄氏度，出口1400度，浓度62wt%')

    def _run(self, **resume):
        from langgraph.types import Command
        graph = build_graph(client_returning(self.GASIFICATION), dry_run=True)
        config = config_for('nv')
        state = initial_state(self.TEXT, scenario_label='g', kind='gibbs',
                              phase='gas', feed_basis='mass_fraction')
        first = graph.invoke(state, config)
        if resume:
            return graph.invoke(Command(resume=resume), config)
        return first

    # ------------------------------------------------------------ the parsing
    def _basis_for(self, answer):
        from reactor_agent.nodes.answers import apply_answers
        merged = apply_answers(dict(self.GASIFICATION),
                               {'q-volumetric-flow': answer})
        return merged.get('normal_volume_basis'), merged

    def test_the_default_sentence_is_understood(self):
        basis, merged = self._basis_for('总进料，0°C/101.325 kPa')
        self.assertEqual(basis['standard_temperature_C'], 0.0)
        self.assertEqual(basis['standard_pressure_kPa'], 101.325)

    def test_the_word_default_is_enough(self):
        basis, _ = self._basis_for('默认')
        self.assertEqual(basis['standard_temperature_C'], 0.0)
        self.assertEqual(basis['standard_pressure_kPa'], 101.325)

    def test_a_standard_temperature_can_be_changed(self):
        for answer in ('20°C', '20℃'):
            with self.subTest(answer=answer):
                basis, _ = self._basis_for(answer)
                self.assertEqual(basis['standard_temperature_C'], 20.0)
                self.assertEqual(basis['standard_pressure_kPa'], 101.325)

    def test_a_temperature_and_a_pressure_together(self):
        basis, _ = self._basis_for('15 C, 1 atm')
        self.assertEqual(basis['standard_temperature_C'], 15.0)
        self.assertAlmostEqual(basis['standard_pressure_kPa'], 101.325, places=3)

    def test_kelvin_is_converted(self):
        basis, _ = self._basis_for('273.15 K')
        self.assertAlmostEqual(basis['standard_temperature_C'], 0.0, places=6)

    def test_a_normal_volume_total_is_kept(self):
        basis, merged = self._basis_for('80000 Nm3/h')
        self.assertEqual(merged['feed_total'], 80000.0)
        self.assertEqual(merged['feed_unit'], 'Nm3/h')
        self.assertEqual(basis['standard_pressure_kPa'], 101.325)

    def test_a_mass_flow_replaces_the_volume(self):
        basis, merged = self._basis_for('49086 kg/h')
        self.assertEqual(merged['feed_total'], 49086.0)
        self.assertEqual(merged['feed_unit'], 'kg/h')
        self.assertIsNone(basis)

    def test_a_refusal_changes_nothing(self):
        for answer in ('不是', 'no idea', ''):
            with self.subTest(answer=answer):
                from reactor_agent.nodes.answers import apply_answers
                notes = []
                merged = apply_answers(dict(self.GASIFICATION),
                                       {'q-volumetric-flow': answer}, note=notes)
                self.assertNotIn('normal_volume_basis', merged)
                self.assertTrue(notes)

    def test_another_stream_is_refused_with_an_explanation(self):
        from reactor_agent.nodes.answers import apply_answers
        notes = []
        merged = apply_answers(dict(self.GASIFICATION),
                               {'q-volumetric-flow': '指出口合成气'}, note=notes)
        self.assertNotIn('normal_volume_basis', merged)
        self.assertTrue(any('其他物流' in n for n in notes), notes)

    def test_a_compiler_question_id_routes_like_the_normaliser_one(self):
        from reactor_agent.nodes.answers import apply_answers
        first = apply_answers(dict(self.GASIFICATION),
                              {'q-volumetric-flow': '默认'})
        second = apply_answers(dict(self.GASIFICATION),
                               {'q-flow-basis-0': '默认'})
        self.assertEqual(first.get('normal_volume_basis'),
                         second.get('normal_volume_basis'))

    def test_a_compiler_temperature_question_writes_the_field(self):
        from reactor_agent.nodes.answers import apply_answers
        merged = apply_answers(dict(self.GASIFICATION), {'q-temp-missing-0': '40 C'})
        self.assertEqual(merged['feed_temperature'], 40.0)

    # ------------------------------------------------------------- the routing
    def test_the_first_pause_asks_both_questions_with_defaults(self):
        state = self._run()
        questions = state['__interrupt__'][0].value['questions']
        by_id = {q['id']: q for q in questions}
        self.assertIn('q-volumetric-flow', by_id)
        self.assertIn('q-coal-definition', by_id)
        self.assertEqual(by_id['q-volumetric-flow']['default'],
                         '总进料，0°C/101.325 kPa')
        self.assertEqual(by_id['q-coal-definition']['default'], '按纯碳处理')
        self.assertEqual(
            [q['field'] for q in questions].count('feeds[0].total_flow_unit'), 1)

    def test_one_round_of_defaults_finishes_the_run(self):
        first = self._run()
        questions = first['__interrupt__'][0].value['questions']
        defaults = {q['id']: q['default'] for q in questions}
        state = self._run(**defaults)
        self.assertFalse(state.get('__interrupt__'))
        self.assertEqual(state['status'], 'READY', state.get('problems'))
        spec = state['cases'][0]['spec']
        self.assertEqual(spec['feeds'][0]['flow_input'], 'normal_volume')
        self.assertEqual(spec['reactor']['solid_carbon'], 'saturation')

    def test_a_negative_answer_does_not_confirm_the_coal(self):
        from reactor_agent.nodes.answers import confirmation_text
        for bad in ('不可以', '不行', 'no'):
            with self.subTest(bad=bad):
                self.assertEqual(confirmation_text({'q-coal-definition': bad},
                                                   self.TEXT), self.TEXT)

    def test_the_default_text_does_confirm_it(self):
        from reactor_agent.nodes.answers import confirmation_text
        text = confirmation_text({'q-coal-definition': '按纯碳处理'}, self.TEXT)
        self.assertIn('纯固体碳', text)


class StateSummary(unittest.TestCase):

    def test_the_summary_is_json_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(TOLUENE_FACTS), dry_run=True,
                                run_root=Path(tmp))
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion'),
                                 config_for('j1'))
        json.dumps(state_summary(state), ensure_ascii=False)

    def test_the_summary_reports_assumptions_and_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(client_returning(INCOMPLETE_FACTS), dry_run=True,
                                run_root=Path(tmp))
            state = graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                               kind='conversion', phase='liquid',
                                               feed_basis='mass_fraction'),
                                 config_for('j2'))
        summary = state_summary(state)
        self.assertIn('blocking_questions', summary)
        self.assertIn('assumptions_we_made', summary)


if __name__ == '__main__':
    unittest.main(verbosity=2)
