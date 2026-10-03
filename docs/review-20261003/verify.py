"""Independent offline review. Mock model/worker only; never connects to HYSYS."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent

def suites():
    commands = [[sys.executable, '-m', 'hysys_tools.selfcheck']]
    modules = ['hysys_tools.test_reliability'] + [
        'reactor_agent.test_' + n for n in
        ['llm', 'extraction', 'normalize', 'selection', 'compiler', 'adapters', 'pipeline', 'graph']]
    commands += [[sys.executable, '-m', 'unittest', n] for n in modules]
    commands += [[sys.executable, 'scripts/test_build_submission.py']]
    logs = []
    for cmd in commands:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        logs.append({'command': cmd, 'exit_code': r.returncode,
                     'stdout': r.stdout, 'stderr': r.stderr})
        print(json.dumps({'suite': cmd[-1], 'exit_code': r.returncode}))
    (OUT / 'suites.json').write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding='utf-8')

def repro():
    from langgraph.types import Command
    from reactor_agent.test_pipeline import (TOLUENE_TEXT, TOLUENE_FACTS,
        BLOCKED_FACTS, client_returning, RecordingAdapter)
    from reactor_agent.graph import build_graph, initial_state, state_summary
    from reactor_agent.pipeline import run_pipeline
    from reactor_agent.extraction import grounding_failures
    from reactor_agent.adapters.hysys_cli import HysysCliAdapter

    results = {}
    def pipeline(facts, text=TOLUENE_TEXT, **kw):
        return run_pipeline(text, client=client_returning(facts), **kw)

    wrong = copy.deepcopy(TOLUENE_FACTS)
    wrong['feed_total'] = 380
    run = pipeline(wrong)
    results['cross_field_number'] = {'grounding_failures': grounding_failures(wrong, TOLUENE_TEXT),
                                   'status': run.status, 'spec': run.spec()}
    wrong = copy.deepcopy(TOLUENE_FACTS)
    wrong['feed_pressure_unit'] = 'bar'
    run = pipeline(wrong)
    results['wrong_unit'] = {'grounding_failures': grounding_failures(wrong, TOLUENE_TEXT),
                            'status': run.status, 'spec': run.spec()}

    with tempfile.TemporaryDirectory() as tmp:
        invented = dict(TOLUENE_FACTS, feed_total=12345)
        adapter = RecordingAdapter()
        graph = build_graph(client_returning(invented), adapter=adapter,
                            run_root=Path(tmp), dry_run=False)
        config = {'configurable': {'thread_id': 'empty-answer'}}
        before = graph.invoke(initial_state(TOLUENE_TEXT), config)
        after = graph.invoke(Command(resume={'q-ungrounded:feed_total': 'not a number'}), config)
        results['invalid_answer_bypass'] = {
            'before_status': before.get('status'), 'before_ungrounded': before.get('ungrounded'),
            'before_interrupt': bool(before.get('__interrupt__')),
            'after_status': after.get('status'), 'after_ungrounded': after.get('ungrounded'),
            'mock_calls': len(adapter.calls), 'executed_spec': adapter.calls[0]['spec'] if adapter.calls else None,
            'final_summary': state_summary(after), 'explanation': after.get('explanation')}

    coal = copy.deepcopy(BLOCKED_FACTS)
    coal['feed_unit'] = 'kg/h'
    coal_text = '水煤浆气化，煤炭和水，流量80000kg/h，62wt%，压力40bar，进料40摄氏度，出口1400度，考虑副反应'
    graph = build_graph(client_returning(coal))
    state = graph.invoke(initial_state(coal_text, kind='gibbs', feed_basis='mass_fraction'),
                         {'configurable': {'thread_id': 'coal'}})
    results['hidden_coal_question'] = {'status': state.get('status'), 'blocking': state.get('blocking'),
        'interrupt': bool(state.get('__interrupt__')), 'explanation': state.get('explanation'),
        'decision': state.get('decision')}

    smr = copy.deepcopy(TOLUENE_FACTS)
    smr.update(species=['甲烷','水','一氧化碳','氢气','二氧化碳'],
        feed_composition=[{'name':'甲烷','fraction':1},{'name':'水','fraction':2.7}],
        composition_basis='molar_ratio', feed_total=3700, feed_unit='kmol/h',
        feed_temperature=520, feed_temperature_unit='C', feed_pressure=13.5,
        feed_pressure_unit='bar', conversion_percent=None, conversion_basis='',
        outlet_temperatures=[710,600], outlet_temperature_unit='C',
        reactions=[{'name':'SMR','reversible':True,'species':[
            {'name':'甲烷','coefficient':-1},{'name':'水','coefficient':-1},
            {'name':'一氧化碳','coefficient':1},{'name':'氢气','coefficient':3}]},
            {'name':'WGS','reversible':True,'species':[
            {'name':'一氧化碳','coefficient':-1},{'name':'水','coefficient':-1},
            {'name':'二氧化碳','coefficient':1},{'name':'氢气','coefficient':1}]}])
    smr_text='甲烷蒸汽重整，甲烷和水摩尔比1:2.7，流量3700kmol/h，进料520C，压力13.5bar，两个可逆反应，出口710C和600C'
    default_run = pipeline(smr, smr_text)
    explicit_run = pipeline(smr, smr_text, kind='gibbs')
    results['autoselection_default'] = {'default_status':default_run.status,
        'default_problems':default_run.problems, 'explicit_gibbs_status':explicit_run.status,
        'explicit_problems':explicit_run.problems}
    graph = build_graph(client_returning(smr))
    custom_state = graph.invoke(initial_state(smr_text, feed_basis='mass_fraction'),
        {'configurable': {'thread_id': 'custom-smr'}})
    results['custom_graph_basis'] = {'status':custom_state.get('status'),
        'problems':custom_state.get('problems'), 'specs':custom_state.get('cases')}
    chosen_text = smr_text.replace('流量3700kmol/h', '进料流量可以自定')
    chosen = pipeline(smr, chosen_text, kind='gibbs', allowed_ungrounded={'feed_total'})
    results['model_chosen_flow_assumption'] = {'status':chosen.status,
        'assumptions':chosen.spec().get('assumptions') if chosen.spec() else None}
    kinetic_text = TOLUENE_TEXT + '。已给出液相动力学：r=k*C_Toluene，k=0.1 s^-1，釜体积10 m3。'
    kinetic = pipeline(TOLUENE_FACTS, kinetic_text, phase='liquid')
    results['kinetics_lost'] = {'status':kinetic.status,
        'rule':kinetic.decision.rule_id,'explanation':kinetic.decision.explanation}
    pure_coal = pipeline(coal, coal_text + '，煤按纯碳处理。', kind='gibbs', feed_basis='mass_fraction')
    results['solid_capability'] = {'status':pure_coal.status,
        'capability':pure_coal.decision.capability_status}
    with tempfile.TemporaryDirectory() as tmp:
        class ReplayAdapter(RecordingAdapter):
            def run_case(self, spec, case_id, run_root, attempt=1):
                outcome = super().run_case(spec, case_id, run_root, attempt)
                label = 'smr-710C' if len(self.calls) == 1 else 'smr-600C'
                evidence = ROOT / 'tool-layer-runs/acceptance-20261002-144131-d442afae' / label / 'result.json'
                outcome.result = json.loads(evidence.read_text(encoding='utf-8'))
                return outcome
        adapter = ReplayAdapter()
        graph = build_graph(client_returning(smr), adapter=adapter, dry_run=False, run_root=Path(tmp))
        state = graph.invoke(initial_state(smr_text, kind='gibbs'),
                             {'configurable': {'thread_id':'replayed-results'}})
        results['result_reporting'] = {'mode':'offline replay of historical tool result.json; no HYSYS execution',
            'summary':state_summary(state), 'explanation':state.get('explanation')}
    with tempfile.TemporaryDirectory() as tmp:
        adapter = RecordingAdapter(['TIMEOUT', 'PASS'])
        run = pipeline(smr, smr_text, kind='gibbs', dry_run=False, adapter=adapter, run_root=Path(tmp))
        results['timeout_continues'] = {'status':run.status, 'mock_calls':len(adapter.calls),
                                      'statuses':[r.status for r in run.executions]}
    with tempfile.TemporaryDirectory() as tmp:
        adapter = RecordingAdapter()
        first = pipeline(TOLUENE_FACTS, dry_run=False, adapter=adapter, run_root=Path(tmp))
        second = pipeline(TOLUENE_FACTS, dry_run=False, adapter=adapter, run_root=Path(tmp))
        results['cached_pipeline_status'] = {'first':first.status,'second':second.status,
            'total_mock_calls':len(adapter.calls),'second_problems':second.problems}
    with tempfile.TemporaryDirectory() as tmp:
        def fake_worker(command, **kwargs):
            folder = Path(command[command.index('--folder') + 1])
            (folder / 'result.json').write_text('{"status":"PASS"}', encoding='utf-8')
            return subprocess.CompletedProcess(command, 1, b'', b'synthetic worker error')
        adapter = HysysCliAdapter(ROOT, runner=fake_worker, use_lock=False)
        outcome = adapter.run_case({}, 'false-pass', Path(tmp))
        results['nonzero_worker_pass'] = {'status':outcome.status, 'exit_code':outcome.exit_code,
            'passed':outcome.passed,'case_file':outcome.case_file}
    (OUT / 'reproductions.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, result in results.items():
        print(json.dumps({name:result}, ensure_ascii=True))
    hashes = json.loads((ROOT / 'tool-layer-runs/acceptance-20261002-144131-d442afae/source_hashes.json').read_text(encoding='utf-8'))
    check = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
             for name, expected in hashes.items()}
    (OUT / 'frozen-tool-hashes.json').write_text(json.dumps(check, indent=2), encoding='utf-8')

if __name__ == '__main__':
    if '--suites' in sys.argv:
        suites()
    else:
        repro()
