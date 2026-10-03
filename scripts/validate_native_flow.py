"""Run on the remote HYSYS Windows workstation, never via SSH to another host."""
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hysys_tools.core import write_json
from hysys_tools.capabilities import TOOL_REVISION
from hysys_tools.examples import gasification_saturation_spec
from hysys_tools.remote_check import run_worker, workstation_lock


def main():
    folder = ROOT / 'tool-layer-runs' / ('native-flow-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    folder.mkdir(parents=True)
    print('Evidence: ' + str(folder), flush=True)
    summary = {'tool_revision':TOOL_REVISION, 'status':'FAILED', 'checks':[]}
    write_json(folder / 'spec.json', gasification_saturation_spec())
    write_json(folder / 'source_hashes.json', {
        str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / 'hysys_tools').glob('*.py')})
    try:
        with workstation_lock():
            for label, args, timeout in [
                ('offline', ['-m','unittest','hysys_tools.test_native_flow','hysys_tools.test_phase1_gate','hysys_tools.test_saturation'], 120),
                ('health', ['-m','hysys_tools','--health-check'],30),
                ('gasification', ['-m','hysys_tools','--spec',str(folder/'spec.json'), '--folder',str(folder/'gasification')],180)]:
                record = run_worker(args, folder / label, label, timeout)
                summary['checks'].append(record)
                if record['exit_code'] != 0 or record['status'] == 'TIMEOUT':
                    raise RuntimeError(label + ' failed; inspect logs before another run')
            result = json.loads((folder/'gasification/result.json').read_text(encoding='utf-8'))
            # `case_file` is a bare file name; resolving it against the CWD points at
            # the project root, where it never is. The case lives beside its result.
            # `remote_check.py` already joins the folder this way.
            case = folder / 'gasification' / (result.get('case_file') or '')
            if result.get('status') != 'PASS' or not result.get('native_flow_readback') or not case.is_file() or case.stat().st_size == 0:
                raise RuntimeError('Missing PASS, native readback or saved HSC evidence')

            # A Gibbs outlet can balance perfectly and still be thermodynamically
            # impossible - the first run of this scenario produced exactly that, with
            # every oxygen atom as CO and every hydrogen atom as CH4 at 1400 C. Atom
            # and mass conservation cannot see it, so PASS is gated on the equilibrium
            # check rather than on conservation alone.
            equilibrium = (result.get('checks') or {}).get('gibbs_equilibrium') or {}
            if equilibrium.get('verdict') != 'CONSISTENT':
                raise RuntimeError(
                    'Gibbs outlet is not thermodynamically consistent (%s); refusing '
                    'to report it as a result. Detail: %s'
                    % (equilibrium.get('verdict') or 'check missing', equilibrium))
            # Two more conditions for a solid-carbon case, each a separate way to be
            # wrong that the equilibrium check cannot see: the carbon must have left as
            # a solid (not dissolved in the gas), and the duty must agree with an
            # energy balance built from independent enthalpy data. A missing block is
            # a failure, never a pass.
            # The spec solves the gasification by imposed graphite saturation; its own
            # block must be present, so a run that silently took another path fails.
            if not result.get('solid_carbon_saturation'):
                raise RuntimeError('solid_carbon_saturation block missing from the result')
            required = {'condensed_phase_location': 'CONDENSED_PHASE_ONLY',
                        'independent_duty': 'CONSISTENT'}
            for name, wanted in required.items():
                block = (result.get('checks') or {}).get(name) or {}
                if block.get('verdict') != wanted:
                    raise RuntimeError('%s is %s, not %s; refusing to report. Detail: %s'
                                       % (name, block.get('verdict') or 'missing',
                                          wanted, block))
            closed = [s for s in result.get('steps', []) if s.get('step') == 'close_saved_case']
            if not closed or closed[-1].get('detail', {}).get('result') != 'OK':
                raise RuntimeError('Saved case cleanup was not confirmed')
            health = run_worker(['-m','hysys_tools','--health-check'], folder/'health-after', 'health-after',30)
            summary['checks'].append(health)
            if health['exit_code'] != 0 or health['status'] == 'TIMEOUT':
                raise RuntimeError('Final health check failed')
            summary['status'] = 'PASS_WITH_PURE_CARBON_ASSUMPTION'
    except Exception as exc:
        summary['error'] = str(exc)
    write_json(folder/'summary.json', summary)
    archive = shutil.make_archive(str(folder), 'zip', root_dir=folder.parent, base_dir=folder.name)
    print('Status: ' + summary['status'])
    print('Return evidence ZIP: ' + archive)
    return 0 if summary['status'].startswith('PASS') else 1


if __name__ == '__main__':
    sys.exit(main())
