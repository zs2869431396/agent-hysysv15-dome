import copy
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace

from .core import SpecError, StepLog, canonical
from .equilibrium import derive, fit, lnk, check_outlet, assert_readback, R
from .examples import equilibrium_reforming_spec
from .precheck import validate_spec
from .validate import ResultCheckError, verify_case
from .reactor import CaseBuilder
from .selfcheck import FakeApplication, PythonComStub, Win32ComStub


class Collection:
    def __init__(self, values): self.values=values; self.Count=len(values)
    def Item(self, i): return self.values[i]


class EquilibriumIntegration(unittest.TestCase):
    def test_recovers_known_curve_at_unseen_temperatures(self):
        expected=[3.,-4000.,.5]
        c=fit([(t,lnk(expected,t)) for t in range(700,1101,50)])
        for t in (715,893,1007): self.assertAlmostEqual(lnk(c,t),lnk(expected,t),places=8)

    def test_generic_stoichiometry_and_pressure_reference(self):
        package=SimpleNamespace(Components=Collection([
            SimpleNamespace(Name='Hydrogen',EvaluateGibbs=lambda t:0.),
            SimpleNamespace(Name='Methane',EvaluateGibbs=lambda t:-R*t*(3.-4000./t+.5*math.log(t))),
        ]))
        result=derive(package,{'Hydrogen':-2,'Methane':1},900.)
        expected=3.-4000./900+.5*math.log(900)-math.log(1.01325)
        self.assertAlmostEqual(result['lnK_exact_bar'],expected)
        self.assertEqual(result['delta_n_gas'],-1)
        self.assertEqual(result['fit_range_K'],[750.,1050.])

    def test_bad_fit_rejected(self):
        package=SimpleNamespace(Components=Collection([
            SimpleNamespace(Name='Methane',EvaluateGibbs=lambda t:R*t*math.sin(t)),
            SimpleNamespace(Name='Hydrogen',EvaluateGibbs=lambda t:0.),
        ]))
        with self.assertRaisesRegex(SpecError,'residual'):
            derive(package,{'Methane':-1,'Hydrogen':2},900.)

    def test_missing_gibbs_species_rejected(self):
        with self.assertRaises(SpecError):
            derive(SimpleNamespace(Components=Collection([])),{'Methane':-1},900)

    def test_bad_readback_rejected(self):
        reaction=SimpleNamespace(EquilibriumConstantParameterArrayValue=[1.]*8)
        with self.assertRaises(SpecError): assert_readback(reaction,[0.]*8)

    def test_production_builder_writes_and_reads_arrays(self):
        import tempfile
        spec=equilibrium_reforming_spec()
        with tempfile.TemporaryDirectory() as tmp:
            app=FakeApplication(); case=app.SimulationCases.Add(str(Path(tmp)/'test.hsc'))
            builder=CaseBuilder(spec,Path(tmp),PythonComStub,Win32ComStub,StepLog(Path(tmp)))
            builder.configure_basis(case,spec['fluid_package'])
            rxset=builder._reaction_set('TEST-EQ')
            result=builder.configure_equilibrium_reactions(rxset,spec['reactions'])
            self.assertEqual(len(result),2)
            self.assertTrue(all(r['final_lnk_source']==1 for r in result))
            self.assertTrue(all(len(r['coefficients_readback'])==8 for r in result))

    def test_remote_probe_outlets_pass_gate(self):
        path=Path(__file__).resolve().parents[1]/'probe-runs/equilibrium-20261003-101446/probe-equilibrium-7.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        for t, block in data['temperatures'].items():
            row=next(a for a in block['attempts'] if a.get('status')=='SOLVED')
            spec=equilibrium_reforming_spec(float(t))
            evidence=[{'reaction':r['name'], 'stoichiometry':{canonical(n):v for n,v in r['stoichiometry'].items()},
                       'temperature_K':float(t)+273.15, 'lnK_exact_bar':math.log(p['k_target'])}
                      for r,p in zip(spec['reactions'],row['reactions'])]
            self.assertEqual(check_outlet(row['solved']['outlet'],evidence)['verdict'],'PASS')
            bad=copy.deepcopy(evidence);bad[0]['lnK_exact_bar']+=1.
            with self.assertRaises(ResultCheckError): check_outlet(row['solved']['outlet'],bad)

    def test_missing_runtime_evidence_cannot_pass(self):
        with self.assertRaises(ResultCheckError):
            verify_case(equilibrium_reforming_spec(),{'methane':1000,'water':2700},{},'equilibrium',0.)

    def test_precheck_supported_and_unsupported_phases(self):
        spec=equilibrium_reforming_spec()
        self.assertTrue(validate_spec(spec)['ok'])
        spec['reactions'][0]['phase']='liquid'
        self.assertFalse(validate_spec(spec)['ok'])


if __name__=='__main__': unittest.main()
