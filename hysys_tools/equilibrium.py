"""Gas-phase ln(K) equations from HYSYS Gibbs data; probe-seven COM contract.

EvaluateGibbs returns J/mol on the verified workstation (standard pressure 1 atm).
The reaction basis is partial pressure in bar. No fitted constants are hardcoded.
"""
import math
from .core import SpecError, canonical

R = 8.314462618
FIT_TOLERANCE = 0.005
QK_TOLERANCE = 0.05


def lnk(coefficients, temperature):
    a, b, c = coefficients[:3]
    return a + b / temperature + c * math.log(temperature)


def fit(points):
    rows = [(1., 1000./t, math.log(t)-7.) for t, _ in points]
    matrix = [[sum(r[i]*r[j] for r in rows) for j in range(3)] +
              [sum(r[i]*y for r, (_, y) in zip(rows, points))] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda i: abs(matrix[i][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        if abs(matrix[col][col]) < 1e-15:
            raise SpecError('singular ln(K) fit')
        for row in range(3):
            if row != col:
                ratio = matrix[row][col] / matrix[col][col]
                for k in range(col, 4):
                    matrix[row][k] -= ratio * matrix[col][k]
    a, b, c = [matrix[i][3]/matrix[i][i] for i in range(3)]
    return [a-7*c, 1000*b, c, 0., 0., 0., 0., 0.]


def derive(package, stoichiometry, target_K):
    if not math.isfinite(target_K) or target_K <= 0:
        raise SpecError('invalid equilibrium target temperature')
    nu = {canonical(n):float(v) for n, v in stoichiometry.items() if v}
    components = {canonical(str(package.Components.Item(i).Name)):package.Components.Item(i)
                  for i in range(int(package.Components.Count))}
    if not nu or not set(nu).issubset(components):
        raise SpecError('equilibrium Gibbs data missing for reaction components')
    dn = sum(nu.values())  # Supported reactions are explicitly all-vapour.
    def exact(t):
        values = {n:float(components[n].EvaluateGibbs(t)) for n in nu}
        if any(not math.isfinite(v) or abs(v+32767.) < .5 for v in values.values()):
            raise SpecError('unknown/nonfinite component Gibbs energy')
        return -sum(nu[n]*g for n,g in values.items())/(R*t) + dn*math.log(1.01325)
    low, high = max(1., target_K-150.), target_K+150.
    training = [(low+(high-low)*i/7, 0.) for i in range(8)]
    training = [(t, exact(t)) for t,_ in training]
    coefficients = fit(training)
    # Check between training points as well as exactly at the operating point.
    verification = [(low+(high-low)*i/28, 0.) for i in range(29)] + [(target_K,0.)]
    residual = max(abs(lnk(coefficients,t)-exact(t)) for t,_ in verification)
    if not math.isfinite(residual) or residual > FIT_TOLERANCE:
        raise SpecError('ln(K) fit residual %.6g exceeds %.6g' % (residual,FIT_TOLERANCE))
    return {'coefficients':coefficients, 'stoichiometry':nu, 'delta_n_gas':dn,
            'temperature_K':target_K, 'fit_range_K':[low,high],
            'fit_max_residual':residual, 'fit_points':[{'T_K':t,'lnK_bar':y} for t,y in training],
            'lnK_exact_bar':exact(target_K), 'basis_units':'bar',
            'gibbs_source':'HYSYS EvaluateGibbs, J/mol, reference pressure 1 atm'}


def assert_readback(reaction, expected):
    actual = [float(v) for v in reaction.EquilibriumConstantParameterArrayValue]
    if len(actual) != 8 or any(not math.isfinite(v) or not math.isclose(v,e,rel_tol=1e-10,abs_tol=1e-9)
                               for v,e in zip(actual,expected)):
        raise SpecError('Equilibrium ln(K) coefficient readback mismatch')
    if int(reaction.LnKSource) != 1 or int(reaction.Basis) != 2 or int(reaction.ReactionPhase) != 0 or str(reaction.BasisUnits2).strip().casefold() != 'bar':
        raise SpecError('Equilibrium source/phase/partial-pressure basis readback mismatch')
    if not math.isclose(float(reaction.TemperatureApproachValue),0.,abs_tol=1e-9):
        raise SpecError('Equilibrium temperature approach must remain zero')
    return actual


def check_outlet(products, evidence):
    from .validate import ResultCheckError
    if not evidence:
        raise ResultCheckError('Equilibrium results need runtime Gibbs/ln(K) evidence')
    vapour = (products or {}).get('VAPOUR',{})
    liquid = (products or {}).get('LIQUID',{})
    if float(liquid.get('molar_flow_kmol_h',0)) > 1e-8:
        raise ResultCheckError('Equilibrium validation currently supports gas-only outlets')
    try:
        temperature = float(vapour['temperature_C']) + 273.15
        pressure = float(vapour['pressure_kPa'])/100.
        fractions = {canonical(n):float(v) for n,v in vapour['mole_fractions'].items()}
        amount = float(vapour['molar_flow_kmol_h'])
    except (KeyError,TypeError,ValueError) as exc:
        raise ResultCheckError('Missing measured vapour state for Q/K') from exc
    if not all(math.isfinite(v) and v>0 for v in (temperature,pressure,amount)) or any(not math.isfinite(v) or v<0 for v in fractions.values()) or not math.isclose(sum(fractions.values()),1.,abs_tol=1e-6):
        raise ResultCheckError('Invalid measured vapour state for Q/K')
    results = []
    for item in evidence:
        if abs(temperature-item['temperature_K']) > .05:
            raise ResultCheckError('Equilibrium measured temperature differs from fitted operating point')
        try:
            lnq = sum(v*math.log(fractions[n]*pressure) for n,v in item['stoichiometry'].items())
        except (KeyError,ValueError) as exc:
            raise ResultCheckError('Missing/zero reaction component in equilibrium outlet') from exc
        delta = lnq-item['lnK_exact_bar']
        if not math.isfinite(delta) or abs(delta) >= QK_TOLERANCE:
            raise ResultCheckError('%s: abs(ln(Q/K))=%.6g exceeds %.6g' % (item['reaction'],abs(delta),QK_TOLERANCE))
        results.append({'reaction':item['reaction'], 'Q_over_K':math.exp(delta),
                        'ln_Q_over_K':delta, 'lnQ_bar':lnq, 'lnK_bar':item['lnK_exact_bar'],
                        'temperature_K':temperature, 'pressure_bar':pressure, 'verdict':'PASS'})
    return {'verdict':'PASS', 'ln_ratio_tolerance':QK_TOLERANCE,'reactions':results}
