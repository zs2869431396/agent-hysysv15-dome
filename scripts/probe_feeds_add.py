"""Isolate why reactor.Feeds.Add fails on this workstation.

Three independent blank cases are built, identical except for one variable each,
so the failing condition is identified in a single run instead of by guessing.

  A  IsIgnored = True  -> Feeds.Add        (current tool-layer order)
  B  Feeds.Add         -> IsIgnored = True (hypothesis: holding the operation
                                            ignored is what makes it refuse feeds)
  C  the exact call order of toluene_test.py, which is verified working on this
     machine: IsIgnored = True -> read TypeName -> Feeds.Add -> connect products
     -> EnergyStream -> PressureDrop -> ReactionSet -> HeatFlow

If A fails and B succeeds, holding the operation ignored is the cause.
If A and B both succeed, the difference lies in the wider call sequence, and C
distinguishes "order of IsIgnored" from "order of everything else".

Every case is newly created by this script; no existing case is read or modified.
Console output is ASCII only.
"""
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

COMPONENTS = ['Toluene', 'Benzene', 'o-Xylene', 'm-Xylene', 'p-Xylene']
FEED_FRACTIONS = (1.0, 0.0, 0.0, 0.0, 0.0)


def report(folder, payload):
    path = Path(folder) / 'probe-feeds-add.json'
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding='utf-8')
    except Exception:
        pass


def build_basis(app, tag, folder, counter):
    """Create a blank case with the toluene basis. Returns the case objects."""
    import pythoncom
    import win32com.client
    counter[0] += 1
    target = Path(folder) / ('%02d-%s.hsc' % (counter[0], tag))
    case = app.SimulationCases.Add(str(target))
    if case is None:
        raise RuntimeError('SimulationCases.Add returned no case')
    basis = case.BasisManager
    basis.StartBasisChange()
    packages = basis.FluidPackages
    if int(packages.Count) == 0:
        packages.Add('PROBE-PR')
    package = packages.Item(0)
    package.PropertyPackageName = 'PengRob'
    for name in COMPONENTS:
        package.Components.Add(name)
    readback = [str(package.Components.Item(i).Name)
                for i in range(int(package.Components.Count))]
    manager = basis.ReactionPackageManager
    manager.ReactionSets.Add('PROBE-SET')
    reaction_set = manager.ReactionSets.Item('PROBE-SET')
    manager.Reactions.Add('PROBE-RXN', 'conversionrxn')
    reaction = manager.Reactions.Item('PROBE-RXN')
    for coefficient, component in ((-2.0, 'Toluene'), (1.0, 'Benzene'),
                                   (1 / 3, 'o-Xylene'), (1 / 3, 'm-Xylene'),
                                   (1 / 3, 'p-Xylene')):
        reaction.Reactants.Add(component)
        reaction.Reactants.Item(component).StoichiometricCoefficientValue = coefficient
    import pythoncom
    import win32com.client
    info = reaction._oleobj_.GetTypeInfo()
    for index in range(info.GetTypeAttr().cFuncs):
        desc = info.GetFuncDesc(index)
        if (info.GetNames(desc.memid)[0] == 'BaseComponent'
                and desc.invkind == pythoncom.DISPATCH_PROPERTYPUT):
            type_desc = desc.args[0][0]
            if type_desc[0] == pythoncom.VT_PTR:
                type_desc = type_desc[1]
            ref_name = info.GetRefTypeInfo(type_desc[1]).GetDocumentation(-1)[0]
            if 'reactant' in ref_name.casefold():
                reaction.BaseComponent = reaction.Reactants.Item('Toluene')
            else:
                reaction.BaseComponent = package.Components.Item('Toluene')
            break
    reaction.ConversionCoefficientsValue = win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, (50.0, 0.0, 0.0))
    reaction.ReactionPhase = 5
    reaction_set.ActiveReactions.Add(reaction)
    reaction_set.AssociateFluidPackage(package)
    if not bool(basis.CanEndBasisChange):
        raise RuntimeError('basis incomplete')
    basis.EndBasisChange()
    return case, basis, package, reaction_set, readback


def make_feed(flow):
    import pythoncom
    import win32com.client
    streams = flow.MaterialStreams
    for name in ('FEED', 'VAPOUR', 'LIQUID'):
        streams.Add(name)
    feed = streams.Item('FEED')
    feed.ComponentMolarFraction.Values = win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, FEED_FRACTIONS)
    feed.Temperature.SetValue(380.0, 'C')
    feed.Pressure.SetValue(2500.0, 'kPa')
    feed.MassFlow.SetValue(10000.0, 'kg/h')
    return streams, feed


def variant(app, folder, counter, label):
    """Run one variant and return a structured result."""
    result = {'variant': label, 'status': 'RUNNING'}
    try:
        case, basis, package, reaction_set, readback = build_basis(
            app, label, folder, counter)
        result['components'] = readback
        flow = case.Flowsheet
        streams, feed = make_feed(flow)
        operations = flow.Operations
        operations.Add('RX', 'conversionreactorop')
        reactor = operations.Item('RX')

        if label == 'A_ignored_then_feed':
            reactor.IsIgnored = True
            result['type_name'] = str(reactor.TypeName)
            reactor.Feeds.Add(feed)
        elif label == 'B_feed_then_ignored':
            reactor.Feeds.Add(feed)
            reactor.IsIgnored = True
        elif label == 'C_original_script_order':
            reactor.IsIgnored = True
            result['type_name'] = str(reactor.TypeName)
            reactor.Feeds.Add(feed)
            reactor.VapourProduct = streams.Item('VAPOUR')
            reactor.LiquidProduct = streams.Item('LIQUID')
            flow.EnergyStreams.Add('DUTY')
            reactor.EnergyStream = flow.EnergyStreams.Item('DUTY')
            reactor.PressureDrop.SetValue(0.0, 'kPa')
            reactor.ReactionSet = reaction_set
            reactor.HeatFlow.SetValue(0.0, 'kW')

        result['feed_count'] = int(reactor.Feeds.Count)
        result['status'] = 'FEEDS_ADD_OK'
    except Exception as exc:
        result['status'] = 'FEEDS_ADD_FAILED'
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        result['traceback_tail'] = traceback.format_exc()[-500:]
    return result


def variant_d_tool_layer(folder, counter):
    """Run the tool layer itself on the toluene spec, in this same process.

    This is the decisive comparison: variants A/B/C are hand-written calls that all
    succeeded, so if the tool layer fails here while calling the same HYSYS
    operations, the difference is inside the tool layer and can be diffed directly.
    """
    result = {'variant': 'D_tool_layer_build_case', 'status': 'RUNNING'}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hysys_tools import examples
        from hysys_tools.main import build_case

        spec = examples.toluene_spec()
        sub = Path(folder) / 'tool-layer-case'
        sub.mkdir(parents=True, exist_ok=True)
        import pythoncom
        import win32com.client
        app = win32com.client.GetActiveObject('HYSYS.Application')
        outcome = build_case(spec, sub, pythoncom, win32com, app)
        result['tool_layer_status'] = outcome.get('status')
        result['tool_layer_error'] = outcome.get('error')
        result['tool_layer_steps'] = [s.get('step') for s in outcome.get('steps', [])]
        checks = outcome.get('checks') or {}
        if checks:
            result['element_relative_error'] = checks.get('element_relative_error')
            conversions = [c for c in (checks.get('specified_conversion') or [])
                           if c.get('checked')]
            if conversions:
                result['measured_conversion_percent'] = conversions[0].get(
                    'measured_percent')
        result['status'] = ('TOOL_LAYER_OK'
                            if outcome.get('status') == 'PASS' else 'TOOL_LAYER_FAILED')
    except Exception as exc:
        result['status'] = 'TOOL_LAYER_FAILED'
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        result['traceback_tail'] = traceback.format_exc()[-800:]
    return result


def variant_e_cli_path(folder):
    """Reproduce the CLI path exactly: write the example spec to disk, load it back
    with core.load_spec, then build it.

    Result D used the in-memory spec object and succeeded. This variant uses the
    file route that the CLI actually takes, so if it fails the difference is in the
    spec's JSON round-trip and nothing else.
    """
    result = {'variant': 'E_cli_file_path', 'status': 'RUNNING'}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hysys_tools import core, examples
        from hysys_tools.main import build_case

        spec_dir = Path(folder) / 'spec-from-file'
        spec_dir.mkdir(parents=True, exist_ok=True)
        examples.write_examples(spec_dir)
        spec_path = spec_dir / 'toluene-disproportionation.json'
        spec = core.load_spec(spec_path)
        result['loaded_from'] = str(spec_path)
        result['feeds_spec'] = spec['feeds'][0]
        result['reaction_stoich'] = spec['reactions'][0]['stoichiometry']

        sub = Path(folder) / 'cli-path-case'
        sub.mkdir(parents=True, exist_ok=True)
        import pythoncom
        import win32com.client
        app = win32com.client.GetActiveObject('HYSYS.Application')
        outcome = build_case(spec, sub, pythoncom, win32com, app)
        result['tool_layer_status'] = outcome.get('status')
        result['tool_layer_error'] = outcome.get('error')
        result['tool_layer_steps'] = [s.get('step') for s in outcome.get('steps', [])]
        checks = outcome.get('checks') or {}
        if checks:
            conversions = [c for c in (checks.get('specified_conversion') or [])
                           if c.get('checked')]
            if conversions:
                result['measured_conversion_percent'] = conversions[0].get(
                    'measured_percent')
        result['status'] = ('CLI_PATH_OK'
                            if outcome.get('status') == 'PASS' else 'CLI_PATH_FAILED')
    except Exception as exc:
        result['status'] = 'CLI_PATH_FAILED'
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        result['traceback_tail'] = traceback.format_exc()[-800:]
    return result


def variant_f_subprocess_cli(folder):
    """Reproduce the CLI exactly, as the launcher runs it: a separate process
    invoking ``python -m hysys_tools --spec <file>``.

    Both in-process variants succeeded, so if this one fails the difference is the
    separate process (a fresh COM apartment / first-operation state), not the code.
    """
    import subprocess
    result = {'variant': 'F_subprocess_cli', 'status': 'RUNNING'}
    try:
        spec_dir = Path(folder) / 'spec-for-cli'
        spec_dir.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hysys_tools import examples
        examples.write_examples(spec_dir)

        sub = Path(folder) / 'cli-subprocess-case'
        sub.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-m', 'hysys_tools',
                   '--spec', str(spec_dir / 'toluene-disproportionation.json'),
                   '--folder', str(sub),
                   '--result', str(sub / 'result.json')]
        result['command'] = ' '.join(command)
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=300,
            cwd=str(Path(__file__).resolve().parent))
        result['returncode'] = completed.returncode
        result['stdout_tail'] = (completed.stdout or '')[-800:]
        result['stderr_tail'] = (completed.stderr or '')[-400:]
        result_path = sub / 'result.json'
        if result_path.is_file():
            payload = json.loads(result_path.read_text(encoding='utf-8'))
            result['tool_layer_status'] = payload.get('status')
            result['tool_layer_error'] = payload.get('error')
            result['tool_layer_steps'] = [s.get('step')
                                          for s in payload.get('steps', [])]
        result['status'] = ('SUBPROCESS_OK' if completed.returncode == 0
                            else 'SUBPROCESS_FAILED')
    except Exception as exc:
        result['status'] = 'SUBPROCESS_FAILED'
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        result['traceback_tail'] = traceback.format_exc()[-800:]
    return result


def variant_g_repeated_path(folder):
    """Decisive test of the duplicate-path hypothesis.

    Runs the CLI twice on the SAME case folder. The first run creates the case file;
    the second asks HYSYS for the same path while the first case is still open. If
    the hypothesis holds, the first succeeds and the second fails with the new
    "case is not blank" error (or a bare E_FAIL on older code).
    """
    import subprocess
    result = {'variant': 'G_repeated_case_path', 'status': 'RUNNING', 'runs': []}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from hysys_tools import examples

        spec_dir = Path(folder) / 'spec-for-repeat'
        spec_dir.mkdir(parents=True, exist_ok=True)
        examples.write_examples(spec_dir)
        spec_path = spec_dir / 'toluene-disproportionation.json'

        # One fixed folder, used twice on purpose.
        shared = Path(folder) / 'shared-case-folder'
        shared.mkdir(parents=True, exist_ok=True)

        for attempt in (1, 2):
            run = {'attempt': attempt, 'folder': str(shared)}
            command = [sys.executable, '-m', 'hysys_tools',
                       '--spec', str(spec_path),
                       '--folder', str(shared),
                       '--result', str(shared / ('result-%d.json' % attempt))]
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=300,
                cwd=str(Path(__file__).resolve().parent))
            run['returncode'] = completed.returncode
            run['stdout_tail'] = (completed.stdout or '')[-400:]
            result_path = shared / ('result-%d.json' % attempt)
            if result_path.is_file():
                payload = json.loads(result_path.read_text(encoding='utf-8'))
                run['tool_layer_status'] = payload.get('status')
                run['tool_layer_error'] = payload.get('error')
            run['ok'] = completed.returncode == 0
            result['runs'].append(run)
            report(folder, {'variants': [], 'g_partial': result})

        first_ok = result['runs'][0].get('ok', False)
        second_ok = result['runs'][1].get('ok', False)
        second_error = str(result['runs'][1].get('tool_layer_error') or '')
        result['hypothesis_confirmed'] = bool(first_ok and not second_ok)
        result['status'] = ('REPEAT_PATH_CONFIRMED'
                            if result['hypothesis_confirmed'] else
                            'REPEAT_PATH_BOTH_OK' if (first_ok and second_ok) else
                            'REPEAT_PATH_INCONCLUSIVE')
        result['interpretation'] = (
            'Same path twice: first run OK, second refused. The duplicate case file '
            'path is what breaks the second run, exactly as hypothesised.'
            if result['hypothesis_confirmed'] else
            'Same path twice and both succeeded. The duplicate-path hypothesis is '
            'not confirmed on this build; the earlier failures need another '
            'explanation.'
            if (first_ok and second_ok) else
            'Neither run succeeded; see per-run errors.')
        if 'not blank' in second_error:
            result['new_guard_fired'] = True
    except Exception as exc:
        result['status'] = 'REPEAT_PATH_INCONCLUSIVE'
        result['error'] = '%s: %s' % (type(exc).__name__, exc)
        result['traceback_tail'] = traceback.format_exc()[-800:]
    return result


def main():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()

    folder = Path(__file__).resolve().parent / 'feeds-add-probe-results' / \
        datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    folder.mkdir(parents=True, exist_ok=True)

    payload = {
        'probe': 'feeds_add_isolation',
        'started': datetime.now().isoformat(timespec='seconds'),
        'question': 'Does holding the operation ignored (IsIgnored = True) make '
                    'Feeds.Add fail, or is it the wider call order?',
        'variants': [],
        'notes': [
            'Every case is created by this script; no existing case is touched.',
            'A = current tool-layer order. B = the other hypothesis. '
            'C = the order used by toluene_test.py, which is verified working here.',
        ],
    }

    def say(text):
        print(str(text).encode('ascii', 'replace').decode('ascii'), flush=True)

    try:
        app = win32com.client.GetActiveObject('HYSYS.Application')
    except Exception:
        payload['fatal'] = traceback.format_exc()
        report(folder, payload)
        say('Cannot connect to HYSYS. Is it running?')
        return 1

    counter = [0]
    for label in ('A_ignored_then_feed', 'B_feed_then_ignored',
                  'C_original_script_order'):
        try:
            outcome = variant(app, folder, counter, label)
        except Exception:
            outcome = {'variant': label, 'status': 'SETUP_FAILED',
                       'traceback_tail': traceback.format_exc()[-500:]}
        payload['variants'].append(outcome)
        say('[%s] %s' % (outcome.get('status'), label))
        if outcome.get('error'):
            say('    %s' % outcome['error'][:200])
        report(folder, payload)

    # D: the tool layer itself, same process, same HYSYS instance, in-memory spec.
    outcome = variant_d_tool_layer(folder, counter)
    payload['variants'].append(outcome)
    say('[%s] D_tool_layer_build_case' % outcome.get('status'))
    if outcome.get('error'):
        say('    %s' % outcome['error'][:200])
    if outcome.get('tool_layer_error'):
        say('    %s' % str(outcome['tool_layer_error'])[:200])
    report(folder, payload)

    # E: the tool layer via the CLI's own file route (JSON written then reloaded).
    outcome = variant_e_cli_path(folder)
    payload['variants'].append(outcome)
    say('[%s] E_cli_file_path' % outcome.get('status'))
    if outcome.get('error'):
        say('    %s' % outcome['error'][:200])
    if outcome.get('tool_layer_error'):
        say('    %s' % str(outcome['tool_layer_error'])[:200])
    report(folder, payload)

    # F: the real CLI, in a separate process, exactly as the launcher runs it.
    outcome = variant_f_subprocess_cli(folder)
    payload['variants'].append(outcome)
    say('[%s] F_subprocess_cli' % outcome.get('status'))
    if outcome.get('tool_layer_error'):
        say('    tool layer: %s' % str(outcome['tool_layer_error'])[:200])
    if outcome.get('stderr_tail'):
        say('    stderr: %s' % outcome['stderr_tail'][-200:])
    report(folder, payload)

    # G: same case path used twice, in separate processes. This is the decisive test
    # of the duplicate-path hypothesis for the earlier launcher failures.
    outcome = variant_g_repeated_path(folder)
    payload['variants'].append(outcome)
    say('[%s] G_repeated_case_path (same folder twice)' % outcome.get('status'))
    if outcome.get('interpretation'):
        say('    %s' % outcome['interpretation'])
    if outcome.get('new_guard_fired'):
        say('    the new not-blank guard fired on the second run')
    for run in outcome.get('runs', []):
        say('    attempt %s: %s  %s' % (
            run.get('attempt'), 'OK' if run.get('ok') else 'FAILED',
            str(run.get('tool_layer_error') or '')[:120]))
    report(folder, payload)

    by_label = {v['variant']: v for v in payload['variants']}
    a_ok = by_label.get('A_ignored_then_feed', {}).get('status') == 'FEEDS_ADD_OK'
    b_ok = by_label.get('B_feed_then_ignored', {}).get('status') == 'FEEDS_ADD_OK'
    c_ok = by_label.get('C_original_script_order', {}).get('status') == 'FEEDS_ADD_OK'
    d_ok = by_label.get('D_tool_layer_build_case', {}).get('status') == 'TOOL_LAYER_OK'
    e_status = by_label.get('E_cli_file_path', {}).get('status')
    e_ok = e_status == 'CLI_PATH_OK'
    f_status = by_label.get('F_subprocess_cli', {}).get('status')
    f_ok = f_status == 'SUBPROCESS_OK'
    g = by_label.get('G_repeated_case_path', {})
    g_status = g.get('status')
    g_confirmed = bool(g.get('hypothesis_confirmed'))
    payload['conclusion'] = {
        'A_current_order_ok': a_ok,
        'B_feed_first_ok': b_ok,
        'C_original_order_ok': c_ok,
        'D_tool_layer_inmemory_ok': d_ok,
        'E_tool_layer_from_file_ok': e_ok,
        'F_subprocess_cli_ok': f_ok,
        'G_same_path_twice_confirms_duplicate_path': g_confirmed,
        'verdict': (
            'Duplicate case file path confirmed: with a fresh path every route '
            'succeeds, and reusing the same path refuses the second run. The '
            'launcher failures are explained by reusing tool-layer-runs, and the new '
            'not-blank guard names the cause instead of leaking an E_FAIL.'
            if g_confirmed else
            'All routes succeed and the same path also works twice on this build: the '
            'duplicate-path hypothesis is not confirmed, so the earlier launcher '
            'failures still need an explanation'
            if (d_ok and e_ok and f_ok and g_status == 'REPEAT_PATH_BOTH_OK') else
            'In-process routes succeed but the separate-process CLI fails: process '
            'isolation is the difference'
            if (d_ok and not f_ok) else
            'The spec file route fails while the in-memory route works: the defect is '
            'in the spec JSON round-trip'
            if (d_ok and not e_ok) else
            'inconclusive; see per-variant errors'),
    }
    report(folder, payload)

    say('')
    say('================ CONCLUSION ================')
    say('  A (tool-layer call order)      : %s' % ('OK' if a_ok else 'FAILED'))
    say('  B (Feeds.Add before IsIgnored) : %s' % ('OK' if b_ok else 'FAILED'))
    say('  C (toluene_test.py order)      : %s' % ('OK' if c_ok else 'FAILED'))
    say('  D (tool layer, in-memory spec) : %s' % ('OK' if d_ok else 'FAILED'))
    say('  E (tool layer, spec from file) : %s' % (e_status or 'NOT RUN'))
    say('  F (real CLI, separate process) : %s' % (f_status or 'NOT RUN'))
    say('  G (same case path used twice)  : %s' % (g_status or 'NOT RUN'))
    say('  VERDICT: %s' % payload['conclusion']['verdict'])
    say('  Copy the whole folder back: %s' % folder)
    pythoncom.CoUninitialize()
    return 0


if __name__ == '__main__':
    sys.exit(main())
