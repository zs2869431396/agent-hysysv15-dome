"""Report tests: one renderer, both callers, and the numbers the tool layer computed.

The fixtures are the plan's appendix B, C and D, cut down to the fields the report
reads. They are written out here rather than recomputed, because the point of the tests
is that the report reproduces the tool layer's own numbers - not that it agrees with
itself.
"""
from __future__ import annotations

import unittest

from reactor_agent.adapters.hysys_cli import ExecutionResult
from reactor_agent.report import (
    render_report,
    results_view,
    view_from_run,
    view_from_state,
)

ISOTHERMAL_SCOPE = ('External heat needed to hold the stated outlet temperature. '
                    'Includes sensible heat from the feed inlet temperature and the '
                    'heat of reaction, not only the heat of reaction.')


# ------------------------------------------------- appendix B: gasification
GASIFICATION_RESULT = {
    'status': 'PASS', 'reactor_kind': 'gibbs',
    'case_file': 'agent-gasification-saturation.hsc',
    'feed_molar_flows_kmol_h': {'Carbon': 2533.8002, 'H2O': 1035.4024},
    'outlet': {
        'VAPOUR': {'molar_flow_kmol_h': 2033.879, 'temperature_C': 1400.0,
                   'pressure_kPa': 4000.0,
                   'mole_fractions': {'Carbon': 0.0, 'H2O': 0.005527,
                                      'CO': 0.500119, 'Hydrogen': 0.481726,
                                      'CO2': 0.001716, 'Methane': 0.010912}},
        'LIQUID': {'molar_flow_kmol_h': 1490.9346, 'temperature_C': 1400.0,
                   'pressure_kPa': 4000.0,
                   'mole_fractions': {'Carbon': 1.0, 'H2O': 0.0, 'CO': 0.0,
                                      'Hydrogen': 0.0, 'CO2': 0.0,
                                      'Methane': 0.0}}},
    'component_flows_kmol_h': {'Carbon': 1490.9346, 'H2O': 11.2413, 'CO': 1017.1811,
                               'Hydrogen': 979.7721, 'CO2': 3.49, 'Methane': 22.1945},
    'heat_duty_kW': 84656.26,
    'checks': {
        'worst_element_relative_error': 1.1e-15, 'mass_relative_error': 5.9e-16,
        'reactant_conversion_percent': {'Carbon': 41.1582, 'H2O': 98.9143},
        'co_yield': {'definition': '(n_CO_out - n_CO_in) / n_C_feed * 100%',
                     'co_yield_percent': 40.1445,
                     'carbon_conversion_percent': 41.1582,
                     'dry_outlet_kmol_h': 2022.6377,
                     'co_mole_fraction_dry': 0.502898},
        'heat_duty_scope': ISOTHERMAL_SCOPE,
        'condensed_phase_location': {'verdict': 'CONDENSED_PHASE_ONLY'},
        'gibbs_equilibrium': {'verdict': 'CONSISTENT',
                              'orders_from_equilibrium': 0.00482},
        'independent_duty': {'verdict': 'CONSISTENT',
                             'relative_deviation': -0.00652}},
    'solid_carbon_saturation': {
        'carbon_conversion_x': 0.411582, 'water_limited_x_max': 0.612341,
        'carbon_activity': {'via_methanation': 1.000003, 'via_water_gas': 0.993607,
                            'via_boudouard': 0.994444,
                            'spread_decades': 0.002787},
        'duty_by_reactor_kW': {'conversion': 64348.06, 'gibbs': 20308.2},
        'library_carbon_gibbs_used': False},
    'warnings': [], 'assumptions': [], 'open_questions': [],
}

# ------------------------------------------------- appendix C: reforming
REFORMER_CASES = {
    'smr-600': {
        'heat_duty_kW': 20159.498,
        'reactant_conversion_percent': {'Methane': 30.352385, 'H2O': 20.615656},
        'component_flows_kmol_h': {'Methane': 696.4761, 'H2O': 2143.3773,
                                   'CO': 50.425, 'Hydrogen': 1163.6704,
                                   'CO2': 253.0989},
        'outlet_molar_flow': 4307.0477,
    },
    'smr-710': {
        'heat_duty_kW': 39988.6148,
        'reactant_conversion_percent': {'Methane': 54.035809, 'H2O': 31.929486},
        'component_flows_kmol_h': {'Methane': 459.6419, 'H2O': 1837.9039,
                                   'CO': 218.6201, 'Hydrogen': 1942.8123,
                                   'CO2': 321.738},
        'outlet_molar_flow': 4780.7162,
    },
}

# ------------------------------------------- appendix D: the Q/K contract
EQUILIBRIUM_QK = {
    'verdict': 'PASS', 'ln_ratio_tolerance': 0.05,
    'reactions': [
        {'reaction': 'RXN-1', 'Q_over_K': 1.0003, 'ln_Q_over_K': 0.0003,
         'verdict': 'PASS'},
        {'reaction': 'RXN-2', 'Q_over_K': 0.9998, 'ln_Q_over_K': -0.0002,
         'verdict': 'PASS'}]}


def execution_of(case_id: str, result: dict) -> dict:
    item = ExecutionResult(case_id=case_id, attempt=1, run_dir='runs', status='PASS',
                           exit_code=0, result=result, seconds=1.0,
                           case_file=result.get('case_file'))
    return results_view(item)


def gasification_execution() -> dict:
    return execution_of('gasifier', GASIFICATION_RESULT)


def reformer_execution(case_id: str, values: dict | None = None) -> dict:
    values = values or REFORMER_CASES[case_id]
    temperature = 600.0 if case_id == 'smr-600' else 710.0
    result = {
        'status': 'PASS', 'reactor_kind': 'equilibrium',
        'case_file': 'agent-%s.hsc' % case_id,
        'heat_duty_kW': values['heat_duty_kW'],
        'outlet': {'VAPOUR': {
            'molar_flow_kmol_h': values['outlet_molar_flow'],
            'temperature_C': temperature, 'pressure_kPa': 1350.0,
            'mole_fractions': {
                name: flow / values['outlet_molar_flow']
                for name, flow in values['component_flows_kmol_h'].items()}}},
        'component_flows_kmol_h': dict(values['component_flows_kmol_h']),
        'checks': {
            'reactant_conversion_percent': dict(
                values['reactant_conversion_percent']),
            'heat_duty_scope': ISOTHERMAL_SCOPE,
            'worst_element_relative_error': 1e-14,
            'mass_relative_error': 1e-15,
            'equilibrium_QK': EQUILIBRIUM_QK,
            'independent_duty': {'verdict': 'CONSISTENT',
                                 'relative_deviation': -0.004},
            'gibbs_equilibrium': {'verdict': 'CONSISTENT',
                                  'orders_from_equilibrium': 0.003}},
        'equilibrium_evidence': [
            {'reaction': 'RXN-1', 'fit_max_residual': 2.1e-4,
             'lnK_exact_bar': 3.2, 'basis_units': 'bar'},
            {'reaction': 'RXN-2', 'fit_max_residual': 1.4e-4,
             'lnK_exact_bar': -1.7, 'basis_units': 'bar'}],
        'warnings': ['outlet near the water dew point'],
        'assumptions': [], 'open_questions': [],
    }
    return execution_of(case_id, result)


def gasification_view() -> dict:
    return {
        'status': 'PASS',
        'decision': {'preferred': 'gibbs', 'executed': 'gibbs',
                     'capability': 'verified',
                     'rule': 'gibbs_high_temperature_many_species',
                     'explanation': '气化炉按自由能最小化计算。',
                     'fallback_reason': None, 'substituted': False},
        'blocking': [], 'open_questions': [],
        'assumptions': [
            {'id': 'a-gibbs-candidates', 'field': 'fluid_package.components',
             'value': ['CO2', 'Methane'], 'scope': '按进料元素补齐候选产物',
             'source': 'agent_default', 'accepted': False},
            {'id': 'a-normal-volume', 'field': 'feeds[0].total_flow',
             'value': '80000 Nm3/h @ 0°C/101.325 kPa',
             'scope': '单股混合进料总量', 'source': 'user_answer', 'accepted': True},
            {'id': 'a-solid-carbon-route', 'field': 'reactor.solid_carbon',
             'value': 'saturation', 'scope': '组合流程', 'source': 'derived',
             'accepted': True}],
        'executions': [gasification_execution()],
        'problems': [],
    }


class ReportImprovements(unittest.TestCase):
    def test_component_flows_are_specific_to_each_phase(self):
        text = render_report(gasification_view())
        vapour, solid = text.split('出口流股 LIQUID', 1)
        self.assertIn('由本流股总摩尔流量 × 摩尔分数推算', vapour)
        self.assertIn('CO：%.4f' % (2033.879 * 0.500119), vapour)
        self.assertIn('Carbon：1490.9346', solid)
        self.assertIn('CO：0.0000', solid)

    def test_equilibrium_method_has_a_meaningful_field_and_value(self):
        from reactor_agent.test_normalize import SMR_FACTS
        from reactor_agent.normalize import normalize
        from reactor_agent.pipeline import build_plan
        from reactor_agent.selection import select_reactor
        request, report = normalize(SMR_FACTS, '等温重整工况。', scenario_label='smr', phase='gas')
        decision = select_reactor(request).model_copy(update={'execution_reactor': 'equilibrium'})
        plan = build_plan(request, decision, report)
        assumption = next(a for a in plan.assumptions if a.id == 'a-equilibrium-k')
        text = render_report({'status': 'READY', 'assumptions': [assumption.model_dump()],
                              'decision': {}, 'executions': []})
        self.assertIn('reactions.equilibrium_constant = 由 HYSYS 组分 Gibbs 数据拟合', text)
        self.assertNotIn('reactions = None', text)


class BothCallersAgree(unittest.TestCase):
    """Plan 8.2: the same view must render the same text from either path."""

    def test_state_and_run_views_produce_identical_text(self):
        state = {'status': 'PASS',
                 'decision': gasification_view()['decision'],
                 'blocking': [], 'open_questions': [],
                 'assumptions': gasification_view()['assumptions'],
                 'executions': [gasification_execution()],
                 'problems': []}
        self.assertEqual(render_report(view_from_state(state)),
                         render_report(gasification_view()))

    def test_the_dry_run_sentence_is_present(self):
        text = render_report({'status': 'READY', 'decision': {'executed': 'gibbs',
                                                             'capability': 'verified'},
                              'blocking': [], 'open_questions': [],
                              'assumptions': [], 'executions': [], 'problems': []})
        self.assertIn('dry run', text)


class GasificationReport(unittest.TestCase):

    def _text(self) -> str:
        return render_report(gasification_view())

    def test_the_main_outlet_is_the_vapour(self):
        text = render_report(gasification_view())
        self.assertIn('出口流股 VAPOUR', text)
        self.assertIn('2033.8790', text)

    def test_the_carbon_stream_is_labelled_as_solid_carbon(self):
        text = self._text()
        self.assertIn('LIQUID（固相碳；HYSYS 物流名为 LIQUID，并非液态碳）', text)

    def test_the_oxygen_ceiling_is_reported(self):
        text = self._text()
        self.assertIn('氧平衡上限 40.86%', text)
        self.assertIn('CO 收率主要受进料中的水量限制', text)

    def test_the_duty_scope_is_spelled_out(self):
        text = self._text()
        self.assertIn('不只是反应热', text)

    def test_the_saturated_carbon_block_is_reported(self):
        text = self._text()
        self.assertIn('碳转化率 X = 0.411582', text)
        self.assertIn('boudouard', text)
        self.assertIn('使用 HYSYS 库 Carbon 的 Gibbs 数据：否', text)

    def test_the_tool_layer_warnings_are_listed(self):
        view = gasification_view()
        view['executions'][0]['results']['warnings'] = ['工具层的提醒']
        self.assertIn('工具层提示', render_report(view))

    def test_the_oxygen_ceiling_is_withheld_when_oxygen_is_fed(self):
        """The ceiling is only true while water is the only oxygen carrier."""
        view = gasification_view()
        results = view['executions'][0]['results']
        results['feed_molar_flows_kmol_h'] = {'Carbon': 2533.8002,
                                              'H2O': 1035.4024,
                                              'Oxygen': 100.0}
        self.assertNotIn('氧平衡上限', render_report(view))


class ReformerReport(unittest.TestCase):

    def _view(self, reverse: bool = False) -> dict:
        cold = reformer_execution('smr-600')
        hot = reformer_execution('smr-710')
        if reverse:
            cold['results']['reactant_conversion_percent']['Methane'] = 54.035809
            hot['results']['reactant_conversion_percent']['Methane'] = 30.352385
        return {'status': 'PASS',
                'decision': {'preferred': 'equilibrium', 'executed': 'equilibrium',
                             'capability': 'verified',
                             'rule': 'equilibrium_closed_network',
                             'explanation': '反应网络闭合。', 'fallback_reason': None,
                             'substituted': False},
                'blocking': [], 'open_questions': [], 'assumptions': [],
                'executions': [hot, cold], 'problems': []}

    def test_the_comparison_table_appears(self):
        self.assertIn('工况对比', render_report(self._view()))

    def test_the_conversion_change_is_the_plan_figure(self):
        text = render_report(self._view())
        self.assertIn('+23.68', text)

    def test_the_mechanism_is_explained_when_the_direction_matches(self):
        text = render_report(self._view())
        self.assertIn('吸热', text)
        self.assertIn('水煤气变换放热', text)

    def test_the_reverse_direction_is_flagged_for_review(self):
        text = render_report(self._view(reverse=True))
        self.assertNotIn('重整反应吸热', text)
        self.assertIn('需要核查', text)

    def test_each_reaction_gets_a_q_over_k_line(self):
        text = render_report(self._view())
        self.assertIn('RXN-1：Q/K = 1.0003', text)
        self.assertIn('判定 PASS', text)
        self.assertIn('平衡常数拟合最大残差', text)

    def test_a_single_case_has_no_comparison_section(self):
        view = self._view()
        view['executions'] = view['executions'][:1]
        text = render_report(view)
        self.assertNotIn('工况对比', text)
        self.assertNotIn('温度趋势', text)


class UnsupportedAndBlocked(unittest.TestCase):

    def test_unsupported_says_no_simulation_ran(self):
        text = render_report({
            'status': 'UNSUPPORTED',
            'decision': {'preferred': 'pfr', 'executed': None,
                         'capability': 'unsupported', 'explanation': '没有 PFR 实现。'},
            'blocking': [], 'open_questions': [], 'assumptions': [],
            'executions': [], 'problems': []})
        self.assertIn('没有运行模拟', text)

    def test_a_default_is_shown_with_its_question(self):
        text = render_report({
            'status': 'WAITING_INPUT',
            'decision': {'preferred': 'gibbs', 'capability': 'verified'},
            'blocking': [{'id': 'q-volumetric-flow',
                          'question': '进料流量 80000 Nm3/h 按什么理解？',
                          'default': '总进料，0°C/101.325 kPa'}],
            'open_questions': [], 'assumptions': [], 'executions': [],
            'problems': []})
        self.assertIn('（默认：总进料，0°C/101.325 kPa）', text)


class AdapterDegradesGracefully(unittest.TestCase):

    def test_an_old_result_file_does_not_raise(self):
        """A result written before these fields existed must still explain itself."""
        item = ExecutionResult(case_id='old', attempt=1, run_dir='runs',
                               status='PASS', exit_code=0,
                               result={'status': 'PASS', 'case_file': 'x.hsc',
                                       'outlet': {'VAPOUR': {
                                           'molar_flow_kmol_h': 10.0,
                                           'temperature_C': 500.0,
                                           'pressure_kPa': 200.0,
                                           'mole_fractions': {'CO': 1.0}}}},
                               seconds=0.5)
        results = item.results()
        for key in ('heat_duty_scope', 'equilibrium_QK',
                    'solid_carbon_saturation', 'gibbs_equilibrium',
                    'independent_duty', 'condensed_phase_location',
                    'feed_molar_flows_kmol_h', 'component_flows_kmol_h',
                    'normal_volume_conversion'):
            with self.subTest(key=key):
                self.assertIn(key, results)
                self.assertIsNone(results[key])
        # The fit list is a list field, so "absent" is the empty list.
        self.assertEqual(results['equilibrium_fit'], [])
        render_report({'status': 'PASS',
                       'decision': {'executed': 'gibbs', 'capability': 'verified'},
                       'blocking': [], 'open_questions': [], 'assumptions': [],
                       'executions': [results_view(item)], 'problems': []})

    def test_the_results_shape_is_the_one_the_execute_node_stores(self):
        item = ExecutionResult(case_id='c', attempt=2, run_dir='runs', status='PASS',
                               exit_code=0, result={'status': 'PASS',
                                                    'case_file': 'x.hsc'},
                               seconds=1.0)
        view = results_view(item)
        self.assertEqual(view['case_id'], 'c')
        self.assertEqual(view['attempt'], 2)
        self.assertIn('results', view)
        self.assertIn('streams', view['results'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
