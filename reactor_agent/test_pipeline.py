"""Tests for the pipeline.

The properties worth testing are the refusals: a run with an open blocking question
must execute nothing, a dry run must execute nothing, and a case that already
succeeded must not be run again. Those are the behaviours that protect the
workstation and the operator's time, and none of them need HYSYS to verify.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reactor_agent.adapters.hysys_cli import ExecutionResult, HysysCliAdapter
from reactor_agent.adapters.run_store import RunStore
from reactor_agent.llm import ChatClient, LlmConfig
from reactor_agent.pipeline import (
    FAILED,
    PARTIAL,
    PASS,
    READY,
    UNSUPPORTED,
    WAITING_INPUT,
    run_pipeline,
    write_run_artifacts,
)

# The equation is part of the request, exactly as the exam states it. Without it
# the reaction coefficients have no basis in the text and the grounding check
# refuses them - correctly, which is what the abbreviated fixture was hiding.
TOLUENE_TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料流量10000kg/h，进料温度为380℃，'
                '操作压力2.5MPa，甲苯转化率为50%')

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

# The gasification wording: a volume flow that cannot be converted.
BLOCKED_TEXT = '水煤浆气化，进料为煤炭和水，流量80000Nm3/h，压力40bar，进料温度40摄氏度'

BLOCKED_FACTS = {
    'species': ['碳', '水', '一氧化碳', '氢气'],
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


def client_returning(facts: dict, repeats: int = 8) -> ChatClient:
    body = json.dumps({'choices': [{'finish_reason': 'stop',
                                    'message': {'content': json.dumps(facts)}}],
                       'usage': {}})
    queue = [(200, body)] * repeats
    config = LlmConfig(base='https://example.test/v1', key='sk-' + 'p' * 30,
                       min_interval=0)

    def transport(url, payload, headers, timeout):
        return queue.pop(0)

    return ChatClient(config, transport=transport, sleeper=lambda _s: None)


class RecordingAdapter(HysysCliAdapter):
    """An adapter that records calls instead of starting anything."""

    def __init__(self, statuses=None, **kwargs):
        super().__init__(Path(tempfile.gettempdir()), python='python',
                         use_lock=False, **kwargs)
        self.calls: list[dict] = []
        self.statuses = list(statuses or [])

    def run_case(self, spec, case_id, run_root, attempt=1):
        self.calls.append({'case_id': case_id, 'attempt': attempt,
                           'spec': spec, 'run_root': Path(run_root)})
        status = self.statuses.pop(0) if self.statuses else 'PASS'
        payload = {'status': status, 'case_file': '%s.hsc' % case_id,
                   'checks': {'reactant_conversion_percent': {'Toluene': 50.0}},
                   'heat_duty_kW': 0.0}
        if status != 'PASS':
            payload['error_type'] = 'result_check'
            payload['error'] = 'simulated failure'
        return ExecutionResult(case_id=case_id, attempt=attempt,
                               run_dir=Path(run_root) / case_id, status=status,
                               result=payload, seconds=1.0)


class DryRunTouchesNothing(unittest.TestCase):

    def test_a_dry_run_stops_after_the_pre_check(self):
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(TOLUENE_FACTS), dry_run=True)
        self.assertEqual(run.status, READY)
        self.assertEqual(run.executions, [])
        self.assertIn('dry run', run.explanation)

    def test_the_spec_is_available_even_though_nothing_ran(self):
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(TOLUENE_FACTS), dry_run=True)
        spec = run.spec()
        self.assertIsNotNone(spec)
        self.assertEqual(spec['reactor']['kind'], 'conversion')

    def test_an_adapter_that_was_supplied_is_still_not_called(self):
        adapter = RecordingAdapter()
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(TOLUENE_FACTS),
                           adapter=adapter, dry_run=True)
        self.assertEqual(run.status, READY)
        self.assertEqual(adapter.calls, [])


class BlockedRunsExecuteNothing(unittest.TestCase):
    """The promise that matters most: a question means zero HYSYS calls."""

    def test_an_unconvertible_flow_blocks_without_executing(self):
        adapter = RecordingAdapter()
        run = run_pipeline(BLOCKED_TEXT, scenario_label='gasification', kind='gibbs',
                           phase='gas', feed_basis='mass_fraction',
                           client=client_returning(BLOCKED_FACTS),
                           adapter=adapter, dry_run=False)
        self.assertEqual(run.status, WAITING_INPUT)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(run.executions, [])

    def test_the_question_names_the_problem(self):
        run = run_pipeline(BLOCKED_TEXT, scenario_label='gasification', kind='gibbs',
                           phase='gas', feed_basis='mass_fraction',
                           client=client_returning(BLOCKED_FACTS), dry_run=False)
        joined = ' | '.join(run.blocking_questions)
        self.assertIn('80000', joined)

    def test_the_selection_is_still_reported_when_input_blocks(self):
        """Blocked on input is not the same as unable to choose a model."""
        run = run_pipeline(BLOCKED_TEXT, scenario_label='gasification', kind='gibbs',
                           phase='gas', feed_basis='mass_fraction',
                           client=client_returning(BLOCKED_FACTS), dry_run=False)
        self.assertEqual(run.decision.preferred_reactor, 'gibbs')


class UnsupportedCombination(unittest.TestCase):

    def test_an_unimplemented_reactor_is_reported_not_faked(self):
        facts = dict(TOLUENE_FACTS, conversion_percent=None,
                     conversion_basis='',
                     feed_total=None, feed_compunit=None)
        facts['kinetic_required'] = True
        # A request with neither kinetics nor conversion falls to "insufficient".
        run = run_pipeline('帮我模拟一个反应', scenario_label='vague', kind='conversion',
                           phase='unknown', client=client_returning(facts),
                           dry_run=False)
        self.assertIn(run.status, (UNSUPPORTED, WAITING_INPUT, FAILED))


class Execution(unittest.TestCase):

    def test_a_single_passing_case_reports_pass(self):
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene',
                               kind='conversion', phase='liquid',
                               feed_basis='mass_fraction',
                               client=client_returning(TOLUENE_FACTS),
                               adapter=adapter, run_root=Path(tmp), dry_run=False)
        self.assertEqual(run.status, PASS)
        self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(run.executions[0].passed)

    def test_the_run_root_is_used_as_given_and_gets_a_ledger(self):
        """`run_root` is this run's directory; the caller makes it unique."""
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                         phase='liquid', feed_basis='mass_fraction',
                         client=client_returning(TOLUENE_FACTS),
                         adapter=adapter, run_root=root, dry_run=False)
            self.assertTrue((root / 'ledger.jsonl').is_file())
        self.assertEqual(adapter.calls[0]['run_root'], Path(tmp))

    def test_a_failing_case_reports_failed_with_the_reason(self):
        adapter = RecordingAdapter(statuses=['FAILED'])
        with tempfile.TemporaryDirectory() as tmp:
            run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene',
                               kind='conversion', phase='liquid',
                               feed_basis='mass_fraction',
                               client=client_returning(TOLUENE_FACTS),
                               adapter=adapter, run_root=Path(tmp), dry_run=False)
        self.assertEqual(run.status, FAILED)
        self.assertTrue(run.problems)

    def test_an_unreachable_workstation_stops_the_remaining_cases(self):
        adapter = RecordingAdapter(statuses=['CANNOT_CONNECT_TO_HYSYS', 'PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene',
                               kind='conversion', phase='liquid',
                               feed_basis='mass_fraction',
                               client=client_returning(TOLUENE_FACTS),
                               adapter=adapter, run_root=Path(tmp), dry_run=False)
        self.assertEqual(run.status, FAILED)
        self.assertEqual(len(adapter.calls), 1)      # the second was not attempted
        self.assertTrue(any('did not answer' in p for p in run.problems))


class ResumeDoesNotRedoWork(unittest.TestCase):

    def test_a_case_that_already_passed_is_not_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'toluene'
            first = RecordingAdapter(statuses=['PASS'])
            run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                         phase='liquid', feed_basis='mass_fraction',
                         client=client_returning(TOLUENE_FACTS),
                         adapter=first, run_root=root, dry_run=False)
            self.assertEqual(len(first.calls), 1)

            second = RecordingAdapter(statuses=['PASS'])
            resumed = run_pipeline(TOLUENE_TEXT, scenario_label='toluene',
                                   kind='conversion', phase='liquid',
                                   feed_basis='mass_fraction',
                                   client=client_returning(TOLUENE_FACTS),
                                   adapter=second, run_root=root, dry_run=False)
        self.assertEqual(second.calls, [])
        self.assertTrue(any('already has a result' in p for p in resumed.problems))

    def test_the_ledger_records_the_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'toluene'
            run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                         phase='liquid', feed_basis='mass_fraction',
                         client=client_returning(TOLUENE_FACTS),
                         adapter=RecordingAdapter(statuses=['PASS']),
                         run_root=root, dry_run=False)
            entries = RunStore(root).entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].status, PASS)


class HallucinationBlocksExecution(unittest.TestCase):
    """The anti-hallucination layer must BLOCK, not merely warn.

    A real model, asked for a request with no flow in it, returned `feed_total = 1
    mol/s`. Grounding caught it - but only recorded a problem, so the fabricated
    value stayed in the facts and would have been used to build a case. Reporting
    without blocking is no protection at all.
    """

    NO_FLOW_TEXT = ('甲苯歧化 2C₇H₈ → C₆H₆ + C₈H₁₀，甲苯进料温度380℃，操作压力2.5MPa，甲苯转化率50%')

    HALLUCINATED = dict(TOLUENE_FACTS, feed_total=1.0, feed_unit='mol/s')

    def test_an_invented_flow_blocks_the_run(self):
        adapter = RecordingAdapter()
        run = run_pipeline(self.NO_FLOW_TEXT, scenario_label='toluene',
                           kind='conversion', phase='liquid',
                           feed_basis='mass_fraction',
                           client=client_returning(self.HALLUCINATED),
                           adapter=adapter, dry_run=False)
        self.assertEqual(run.status, WAITING_INPUT)
        self.assertEqual(adapter.calls, [])

    def test_the_question_names_the_ungrounded_field(self):
        run = run_pipeline(self.NO_FLOW_TEXT, scenario_label='toluene',
                           kind='conversion', phase='liquid',
                           feed_basis='mass_fraction',
                           client=client_returning(self.HALLUCINATED), dry_run=False)
        joined = ' | '.join(run.blocking_questions)
        self.assertIn('feed_total', joined)

    def test_a_grounded_value_does_not_block(self):
        """The same field, but this time the user really did state it."""
        adapter = RecordingAdapter(statuses=['PASS'])
        with tempfile.TemporaryDirectory() as tmp:
            run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene',
                               kind='conversion', phase='liquid',
                               feed_basis='mass_fraction',
                               client=client_returning(TOLUENE_FACTS),
                               adapter=adapter, run_root=Path(tmp), dry_run=False)
        self.assertEqual(run.status, PASS)
        self.assertEqual(len(adapter.calls), 1)

    def test_an_exempted_field_does_not_block(self):
        """The exam lets us choose the reformer flow, so it must not be flagged."""
        facts = dict(TOLUENE_FACTS, feed_total=1000.0, feed_unit='kmol/h')
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(facts), dry_run=True,
                           allowed_ungrounded={'feed_total'})
        self.assertEqual(run.status, READY)

    def test_the_ungrounded_list_is_carried_on_the_extraction(self):
        from reactor_agent.extraction import extract_verified
        _, problems = extract_verified(client_returning(self.HALLUCINATED),
                                       self.NO_FLOW_TEXT, 'conversion')
        self.assertTrue(any('may be invented' in p for p in problems))


class ModelFailure(unittest.TestCase):

    def test_no_client_is_a_clean_failure(self):
        run = run_pipeline(TOLUENE_TEXT, client=None, dry_run=True)
        self.assertEqual(run.status, FAILED)
        self.assertIn('no model client', run.problems[0])

    def test_an_unparseable_model_reply_fails_rather_than_guesses(self):
        config = LlmConfig(base='https://example.test/v1', key='sk-' + 'q' * 30,
                           min_interval=0)
        queue = [(200, json.dumps({'choices': [{'message': {'content': 'nope'}}]}))] * 8

        def transport(url, payload, headers, timeout):
            return queue.pop(0)

        client = ChatClient(config, transport=transport, sleeper=lambda _s: None)
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           client=client, dry_run=False)
        self.assertEqual(run.status, FAILED)
        self.assertTrue(run.problems)


class Artifacts(unittest.TestCase):

    def test_the_run_writes_summary_spec_and_request(self):
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(TOLUENE_FACTS), dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            written = write_run_artifacts(run, Path(tmp))
            summary = json.loads((Path(tmp) / 'run.json').read_text('utf-8'))
        self.assertIn('run.json', written)
        self.assertTrue(any(name.startswith('spec-') for name in written))
        self.assertEqual(summary['status'], READY)

    def test_the_summary_lists_the_assumptions_we_made(self):
        run = run_pipeline(TOLUENE_TEXT, scenario_label='toluene', kind='conversion',
                           phase='liquid', feed_basis='mass_fraction',
                           client=client_returning(TOLUENE_FACTS), dry_run=True)
        summary = run.summary()
        self.assertIn('assumptions_we_made', summary)
        self.assertIsInstance(summary['assumptions_we_made'], list)

    def test_the_summary_is_json_safe(self):
        run = run_pipeline(BLOCKED_TEXT, scenario_label='gasification', kind='gibbs',
                           phase='gas', feed_basis='mass_fraction',
                           client=client_returning(BLOCKED_FACTS), dry_run=True)
        json.dumps(run.summary(), ensure_ascii=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
