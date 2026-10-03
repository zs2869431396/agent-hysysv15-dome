"""Report real string-shaped volume conversions and recover execution evidence."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reactor_agent.graph import build_graph, initial_state, open_checkpointer
from reactor_agent.report import render_report
from reactor_agent.test_graph import RecordingAdapter, TOLUENE_FACTS, TEXT, client_returning
from reactor_agent.test_report import GASIFICATION_RESULT, execution_of
from scripts.recover_saved_report import recover


class VolumeConversionReport(unittest.TestCase):
    def view(self, conversion):
        result = copy.deepcopy(GASIFICATION_RESULT)
        result['native_flow_readback'] = {'conversion': conversion}
        return {'status': 'PASS', 'executions': [execution_of('gasifier', result)]}

    def test_the_tool_string_conversion_is_preserved(self):
        text = '80000 Nm3/h / 22.413970 m3/kmol at 0 C / 101.325 kPa = 3569.202667 kmol/h'
        report = render_report(self.view(text))
        self.assertIn(text, report)
        self.assertIn('CO 收率', report)

    def test_the_older_dictionary_shape_is_still_rendered(self):
        report = render_report(self.view({'molar_flow_kmol_h': 3569.2, 'unit': 'kmol/h'}))
        self.assertIn('molar_flow_kmol_h 3569.2000', report)
        self.assertIn('unit kmol/h', report)

    def test_an_absent_conversion_does_not_invent_a_value(self):
        self.assertNotIn('标准体积换算', render_report(self.view(None)))


class SavedExecutionRecovery(unittest.TestCase):
    def test_a_report_failure_recovers_from_sqlite_without_reexecuting(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            adapter = RecordingAdapter()
            client = client_returning(TOLUENE_FACTS)
            with open_checkpointer(folder / 'checkpoints.sqlite') as saver:
                graph = build_graph(client, adapter=adapter, run_root=folder,
                                    dry_run=False, checkpointer=saver)
                with patch('reactor_agent.nodes.explain.render_report',
                           side_effect=AttributeError('reproduced report failure')):
                    with self.assertRaises(AttributeError):
                        graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                                   kind='conversion', feed_basis='mass_fraction'),
                                     {'configurable': {'thread_id': 'toluene'}})
            calls = client.calls
            before = (folder / 'checkpoints.sqlite').read_bytes()
            state, target = recover(folder)
            self.assertEqual(state['status'], 'PASS')
            self.assertEqual(adapter.calls, ['single'])
            self.assertEqual(client.calls, calls)
            self.assertEqual((folder / 'checkpoints.sqlite').read_bytes(), before)
            self.assertTrue((target / 'explanation.txt').is_file())
            self.assertTrue((target / 'state.json').is_file())

    def test_a_missing_checkpoint_stops_without_creating_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(ValueError):
                recover(folder)
            self.assertEqual(list(folder.iterdir()), [])

    def test_a_dry_run_checkpoint_is_not_reported_as_an_executed_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with open_checkpointer(folder / 'checkpoints.sqlite') as saver:
                graph = build_graph(client_returning(TOLUENE_FACTS), run_root=folder,
                                    dry_run=True, checkpointer=saver)
                graph.invoke(initial_state(TEXT, scenario_label='toluene',
                                           kind='conversion', feed_basis='mass_fraction'),
                             {'configurable': {'thread_id': 'toluene'}})
            with self.assertRaises(ValueError):
                recover(folder)
            self.assertFalse((folder / 'recovered-report').exists())
