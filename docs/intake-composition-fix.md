# Intake composition validation — 2026-10-04

The extraction prompt now asks for separate feed substances and explicitly identifies
coal-water slurry concentration as the coal mass percentage. The signed-reaction
example uses ethanol dehydration rather than an exam scenario.

Unresolvable composition entries are collected into one `q-feed-composition`
question, covering the complete component list, proportions and basis. The remaining
valid entries are never compiled as though they describe the whole feed. The legacy
`q-composition-species-*` answer IDs still accept complete composition replacements.

A narrowly explicit `水煤浆进料浓度62wt%` statement provides a source-derived default:
coal 62%, water 38%, mass basis. Missing water, an inverted ratio or a molar basis
blocks compilation until the composition is explicitly confirmed. This does not
confirm coal's pure-carbon representation; `q-coal-definition` remains independent.
Unknown proportions, multiple stated concentrations and explicit additives do not
receive a guessed source-derived default. Defaults use JSON strings and the existing
CLI/web answer path; no Question schema expansion is needed.

Response-only fixtures preserve the original replies, including their mistakes.
Regression tests assert both the structural contract and the final mass fractions.
Accepting the grounded defaults reaches READY with Carbon=0.62, Water=0.38 and the
saturated solid-carbon route. These checks require no model or HYSYS.

## Real-model validation

Endpoint: `https://tokenrhythm.studio/v1/chat/completions`; model: `qwen3.7-flash`.
Single transport attempt per completion, maximum four attempts per intake. Only dry
runs were requested; no HYSYS simulation was executed.

| Scenario | HTTP model requests | Exit | Result |
| --- | --- | --- | --- |
| toluene --no-input | 1 | 0 | READY |
| smr --no-input | 1 | 0 | READY |
| gasification --no-input | 1 | 3 | WAITING_INPUT, three questions |
| gasification --accept-defaults | — | — | Not run: stopped at the failed two-question acceptance check |

All three successful HTTP replies have exactly the 29 contract keys and pass schema
validation. The gasification reply contains only coal at 62%, omitting water. The
new semantic check catches it, rather than treating the sole named component as a
pure feed. Its full composition question includes a grounded default. This is safe
behavior, but does not meet the strict requirement of exactly two initial questions.
The missing-water reply is now an offline fixture; confirming its grounded defaults
was verified offline, without another paid model call.

Artifacts are under `_review/checkpoint-E/live-qwen-network-20261004/` (summary,
verbatim request/response transcripts, CLI logs and specs). An earlier sandboxed
attempt under `live-qwen-20261004/` failed locally with WinError 10013 and received no
HTTP model response; it must not be counted as model quality evidence.

Credentials were supplied only in execution environment variables. No credential
is embedded in scripts or response fixtures. `scripts/validate_live_intake.py`
retains results and stops at the first unexpected result; use a fresh `--out`
directory for each validation run.
