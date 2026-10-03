"""Which flow property and unit does HYSYS actually accept for the gasification feed?

The failure to explain:

    feed.MolarFlow.SetValue(80000, 'Nm3/h')
    -> com_error (-2147352567, 'Exception occurred.')

Three explanations fit that one error, and a single message cannot tell them apart:

  A  `Nm3/h` is not the spelling HYSYS uses for that unit.
  B  `MolarFlow` does not accept a volumetric unit at all, on any stream.
  C  The property and unit are both fine, but the STREAM STATE does not support
     them: `Nm3/h` is a gas standard volume, and the gasification feed is coal plus
     water - a solid and a liquid, with no gas phase to reference.

So this builds two freshly created streams that differ in exactly that one respect,
and runs the same matrix against both:

    GAS    Methane only, 40 C, 40 bar
    SLURRY Carbon + Water at the stated 62/38 mass ratio, 40 C, 40 bar

For every flow-like property it can find, and every candidate unit spelling, it
records whether the value can be read and written.

Reading the result:

  * If GAS accepts `Nm3/h` and SLURRY does not  -> explanation C. The unit exists and
    is spelled right; the stream has no gas basis. Convert the standard volume to a
    molar flow using the stated 0 C / 101.325 kPa basis, and set that with a molar
    unit.
  * If neither accepts `Nm3/h`                  -> explanation A or B. The reported
    list of units that DO work says which spelling to use, or shows that only molar
    and mass units are available.
  * If both accept it                           -> the failure lies elsewhere (stream
    order, an earlier call, or the operation's state), and the raw results printed
    here are the starting point.

Every case is created by this script and closed by it, including on failure. Console
output is ASCII only; the JSON is UTF-8.

Usage on the workstation:
    python scripts\probe_native_flow.py
    python scripts\probe_native_flow.py --folder probe-runs\\native-flow
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------- what to probe

# Properties worth trying. Actual and standard, gas and liquid, because the point is
# to find out which of these the workstation exposes and what each one accepts.
CANDIDATE_PROPERTIES = (
    'MolarFlow', 'MassFlow',
    'ActualVolumeFlow', 'LiquidVolumeFlow', 'GasVolumeFlow',
    'StdIdealLiquidVolumeFlow', 'StdLiquidVolumeFlow',
    'StdGasVolumeFlow', 'IdealGasVolumeFlow',
)

# Unit spellings. `Nm3/h` is the one that failed; the rest are the plausible ways
# HYSYS might name the same thing, plus the molar and mass units that are known to
# work, so the report always contains a control that succeeds.
CANDIDATE_UNITS = (
    'Nm3/h', 'Nm^3/h', 'Nm3/hr', 'N m3/h', 'Nm³/h', 'Normal m3/h',
    'Sm3/h', 'stm3/h', 'std m3/h', 'Sm^3/h',
    'Am3/h', 'm3/h', 'm3/hr',
    'kgmole/h', 'kgmol/h', 'kmol/h', 'gmole/h', 'lbmole/h', 'mole/h',
    'kg/h', 'lb/h',
)

# What the gasification feed actually is.
SLURRY_MASS_FRACTIONS = (('Carbon', 0.62), ('Water', 0.38))
GAS_COMPONENTS = ('Methane',)
GAS_MOLAR_FRACTIONS = (1.0,)

MOLAR_MASS = {'Carbon': 12.011, 'Water': 18.0153, 'Methane': 16.043}

TEMPERATURE_C = 40.0
PRESSURE_BAR = 40.0
PROBE_VALUE = 100.0

# Ideal gas molar volume at 0 C and 101.325 kPa, in cubic metres per kmol. Used only
# to say what the stated basis is worth in molar terms; the probe itself does not
# rely on it.
NORMAL_MOLAR_VOLUME_M3_PER_KMOL = 22.41397


def report(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding='utf-8')
    except Exception:                                   # noqa: BLE001
        pass


def mole_fractions(mass_fractions):
    """Convert a stated mass ratio into mole fractions, for the stream composition."""
    amounts = [(name, mass / MOLAR_MASS[name]) for name, mass in mass_fractions]
    total = sum(amount for _name, amount in amounts)
    return tuple(amount / total for _name, amount in amounts)


def build_case(app, tag: str, folder: Path, counter: list):
    """A blank case with the components the probe needs. Mirrors the tool layer."""
    target = Path(folder) / ('%02d-%s.hsc' % (counter[0], tag))
    counter[0] += 1
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
    names = [n for n, _f in SLURRY_MASS_FRACTIONS] if tag.startswith('slurry') \
        else list(GAS_COMPONENTS)
    for name in names:
        package.Components.Add(name)
    readback = [str(package.Components.Item(i).Name)
                for i in range(int(package.Components.Count))]
    if not bool(basis.CanEndBasisChange):
        raise RuntimeError('basis incomplete; components are %s' % readback)
    basis.EndBasisChange()
    return case, readback


def make_stream(case, tag: str):
    """One material stream, with the composition the probe is about."""
    import pythoncom
    import win32com.client
    flow = case.Flowsheet
    streams = flow.MaterialStreams
    stream = streams.Add('FEED')
    if tag.startswith('slurry'):
        fractions = mole_fractions(SLURRY_MASS_FRACTIONS)
    else:
        fractions = GAS_MOLAR_FRACTIONS
    stream.ComponentMolarFraction.Values = win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, fractions)
    stream.Temperature.SetValue(TEMPERATURE_C, 'C')
    stream.Pressure.SetValue(PRESSURE_BAR, 'bar')
    return stream


def flow_like_attributes(stream):
    """Every attribute whose name suggests a flow, so nothing is missed by hand."""
    found = []
    for name in dir(stream):
        if name.startswith('_'):
            continue
        lowered = name.casefold()
        if 'flow' in lowered or 'volume' in lowered:
            found.append(name)
    return sorted(found)


def probe_variable(variable, unit: str, value: float) -> dict:
    """Try to write and read one unit on one variable."""
    entry = {'unit': unit, 'set': None, 'readback': None, 'error': None}
    try:
        variable.SetValue(value, unit)
        entry['set'] = 'ok'
    except Exception as exc:                            # noqa: BLE001
        entry['set'] = 'failed'
        entry['error'] = _short(exc)
        return entry
    try:
        entry['readback'] = float(variable.GetValue(unit))
    except Exception as exc:                            # noqa: BLE001
        entry['readback_error'] = _short(exc)
    return entry


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 300 else text[:300] + '...'


def probe_stream(stream, tag: str) -> dict:
    """The whole matrix for one stream."""
    result = {'stream': tag, 'attributes': flow_like_attributes(stream),
              'properties': {}}
    for prop in CANDIDATE_PROPERTIES:
        entry: dict = {'present': False, 'units': {}}
        try:
            variable = getattr(stream, prop)
        except Exception as exc:                        # noqa: BLE001
            entry['absent_reason'] = _short(exc)
            result['properties'][prop] = entry
            continue
        entry['present'] = True
        for unit in CANDIDATE_UNITS:
            entry['units'][unit] = probe_variable(variable, unit, PROBE_VALUE)
        result['properties'][prop] = entry
    return result


def summarise(probe: dict) -> dict:
    """Which units worked, per property, so the answer is readable at a glance."""
    out = {}
    for prop, entry in probe['properties'].items():
        if not entry.get('present'):
            continue
        working = sorted(unit for unit, outcome in entry['units'].items()
                         if outcome['set'] == 'ok')
        out[prop] = working
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default=None,
                        help='where to put the probe cases and the report')
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    project = Path(__file__).resolve().parent.parent
    folder = Path(args.folder) if args.folder else (
        project / 'probe-runs' / ('native-flow-%s' % stamp))
    folder.mkdir(parents=True, exist_ok=True)

    payload: dict = {
        'probe': 'native-flow',
        'started': datetime.now().isoformat(timespec='seconds'),
        'folder': str(folder),
        'basis': {
            'temperature_C': TEMPERATURE_C,
            'pressure_bar': PRESSURE_BAR,
            'probe_value': PROBE_VALUE,
            'gas_components': list(GAS_COMPONENTS),
            'slurry_mass_fractions': [list(item) for item in SLURRY_MASS_FRACTIONS],
            'slurry_mole_fractions': list(mole_fractions(SLURRY_MASS_FRACTIONS)),
        },
        'normal_volume_note': (
            'Ideal gas molar volume at 0 C and 101.325 kPa is %.5f m3/kmol; '
            '80000 Nm3/h is therefore %.2f kmol/h.'
            % (NORMAL_MOLAR_VOLUME_M3_PER_KMOL,
               80000.0 / NORMAL_MOLAR_VOLUME_M3_PER_KMOL)),
        'streams': {},
        'summary': {},
        'status': 'RUNNING',
    }

    try:
        import win32com.client
        app = win32com.client.GetActiveObject('HYSYS.Application')
    except Exception as exc:                            # noqa: BLE001
        payload['status'] = 'CANNOT_CONNECT_TO_HYSYS'
        payload['error'] = _short(exc)
        report(folder / 'probe-native-flow.json', payload)
        print('CANNOT_CONNECT_TO_HYSYS; start HYSYS and dismiss any dialogs.')
        return 2

    counter = [1]
    created = []
    try:
        for tag in ('gas', 'slurry'):
            entry: dict = {'tag': tag, 'status': 'RUNNING'}
            payload['streams'][tag] = entry
            try:
                case, readback = build_case(app, tag, folder, counter)
                created.append(case)
                entry['components'] = readback
                stream = make_stream(case, tag)
                probe = probe_stream(stream, tag)
                entry.update(probe)
                entry['status'] = 'OK'
                entry['summary'] = summarise(probe)
            except Exception as exc:                    # noqa: BLE001
                entry['status'] = 'FAILED'
                entry['error'] = _short(exc)
                entry['traceback'] = traceback.format_exc()[-1500:]
        payload['status'] = 'COMPLETED'
    finally:
        # Never leave a case open: a previous incident was caused by 96 leftover
        # cases, which made an unrelated run return nonsense.
        for case in created:
            try:
                case.Close(False)
            except Exception:                           # noqa: BLE001
                pass

    payload['finished'] = datetime.now().isoformat(timespec='seconds')
    report(folder / 'probe-native-flow.json', payload)

    # ------------------------------------------------------------- the answer
    print('probe written: %s' % (folder / 'probe-native-flow.json'))
    for tag, entry in payload['streams'].items():
        print('')
        print('stream %s: %s' % (tag, entry.get('status')))
        if entry.get('status') != 'OK':
            print('  error: %s' % entry.get('error'))
            continue
        for prop, units in (entry.get('summary') or {}).items():
            printable = [u.encode('ascii', 'replace').decode('ascii') for u in units]
            print('  %-26s accepts: %s' % (prop, ', '.join(printable) or '(none)'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
