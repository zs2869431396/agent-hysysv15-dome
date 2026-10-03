"""Data contracts for the three layers, validated by pydantic.

Why pydantic rather than plain dataclasses: the LLM writes these structures, and
the single most common failure in this project has been a field arriving with the
wrong type. pydantic rejects that at the boundary instead of letting it travel
into a spec, and `model_json_schema()` gives the exact JSON Schema a model needs
for structured output, so the contract and the prompt cannot drift apart.

Three layers, and the reason they are separate (plan 5.3):

    ProcessRequest  what the user said, plus where each field came from. Missing
                    fields are kept missing - that is the signal to ask a
                    question rather than to invent a value.
    ModelingPlan    the modelling decision: reactor, species, thermal boundary,
                    assumptions, one entry per operating case.
    ToolSpec        plain dict in hysys-agent/spec/1 form, built by compiler.py
                    and then checked by hysys_tools.precheck.

Nothing the model produces becomes a COM call, a path or a process argument
without being compiled and pre-checked by deterministic Python in between.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Where a value came from. The distinction matters for the report and for the
# assumption audit: something the exam explicitly leaves to us ("进料流量可以自定")
# is a very different thing from a number we chose ourselves.
ValueSource = Literal[
    'exam_explicit',    # stated in the exam text
    'user_text',        # stated by the user in this request
    'user_answer',      # given in reply to a follow-up question
    'exam_allowed',     # the exam explicitly lets us choose it
    'agent_default',    # we chose it; must be shown in the report
    'derived',          # computed from other inputs (e.g. unit conversion)
    'historical',       # taken from a previously verified case
]

# Reactors the exam's selection logic (section 8) names.
ReactorType = Literal['conversion', 'equilibrium', 'gibbs', 'cstr', 'pfr', 'unsupported']

# verified   - real HYSYS run exists for this combination on this workstation
# experimental - the code path exists but no accepted real run
# unsupported - the tool layer refuses or has no implementation
CapabilityStatus = Literal['verified', 'experimental', 'unsupported']

TaskStatus = Literal[
    'WAITING_INPUT', 'READY', 'RUNNING', 'PASS', 'PARTIAL', 'FAILED', 'UNSUPPORTED']


class Strict(BaseModel):
    """Reject unknown keys so a model cannot smuggle in a field we ignore."""

    model_config = ConfigDict(extra='forbid')


# --------------------------------------------------------------- user intent

class ReactionSpec(Strict):
    """One reaction as the user described it."""

    name: str = ''
    # Negative coefficient = reactant, positive = product. Kept as given, because
    # element balance is checked by hysys_tools, not here.
    stoichiometry: dict[str, float]
    reversible: bool | None = None
    source_text: str = ''

    @field_validator('stoichiometry')
    @classmethod
    def _non_empty(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError('a reaction needs a non-empty stoichiometry')
        return value


class FeedSpec(Strict):
    """One feed stream.

    `total_flow_unit` is stored verbatim, including units the tool layer refuses.
    A "Nm3/h" total is not corrected here: the request layer's job is to record
    what was asked, and the ambiguity has to surface as a blocking question rather
    than be silently converted into something plausible.
    """

    name: str = 'FEED'
    basis: Literal['molar_fraction', 'mass_fraction', 'molar_flow', 'mass_flow'] = \
        'molar_fraction'
    fractions: dict[str, float] = Field(default_factory=dict)
    flows: dict[str, float] = Field(default_factory=dict)
    total_flow: float | None = None
    total_flow_unit: str = 'kmol/h'
    temperature: float | None = None
    temperature_unit: str = 'C'
    pressure: float | None = None
    pressure_unit: str = 'kPa'
    source_text: str = ''

    @model_validator(mode='after')
    def _unit_matches_basis(self) -> FeedSpec:
        """Reject a unit that cannot possibly express the chosen basis.

        The default unit is kmol/h, so a request written as `basis='mass_flow'`
        without also stating a mass unit would otherwise travel all the way to the
        tool layer and fail there. Catching it at the contract boundary means the
        intake step learns about it while it can still ask the user.
        """
        from hysys_tools.core import MASS_FLOW_UNITS, MOLAR_FLOW_UNITS

        unit = self.total_flow_unit.strip().casefold()
        if self.basis == 'mass_flow' and unit not in MASS_FLOW_UNITS:
            raise ValueError(
                'basis "mass_flow" needs a mass unit for total_flow_unit; got %r. '
                'Allowed: %s' % (self.total_flow_unit, sorted(MASS_FLOW_UNITS)))
        if self.basis == 'molar_flow' and unit not in MOLAR_FLOW_UNITS:
            raise ValueError(
                'basis "molar_flow" needs a molar unit for total_flow_unit; got %r. '
                'Allowed: %s' % (self.total_flow_unit, sorted(MOLAR_FLOW_UNITS)))
        return self


class KineticData(Strict):
    """Full rate law plus the equipment data a rate-based reactor would need.

    Deliberately strict about emptiness: `is_complete()` gates the CSTR/PFR
    branch, and the plan warns that "管式/釜式" alone must not be treated as
    kinetic data (plan 5.4 step 2).
    """

    rate_law: str = ''
    pre_exponential: float | None = None
    activation_energy_J_mol: float | None = None
    reaction_order: dict[str, float] = Field(default_factory=dict)
    reactor_volume_m3: float | None = None
    catalyst_mass_kg: float | None = None
    residence_time_s: float | None = None
    source_text: str = ''

    def is_complete(self) -> bool:
        """True only when a rate law AND some sizing basis are both present."""
        has_law = bool(self.rate_law.strip()) or (
            self.pre_exponential is not None
            and self.activation_energy_J_mol is not None)
        has_size = any(v is not None for v in (
            self.reactor_volume_m3, self.catalyst_mass_kg, self.residence_time_s))
        return bool(has_law and has_size)


class ConversionConstraint(Strict):
    """A conversion figure the user gave, and what it is measured against."""

    reaction: str = ''
    percent: float
    base_component: str = ''
    source_text: str = ''

    @field_validator('percent')
    @classmethod
    def _percent_range(cls, value: float) -> float:
        # 0.5 meaning 50% has been a real source of confusion; reject it loudly
        # rather than silently simulating a 0.5% conversion.
        if not 0 < value <= 100:
            raise ValueError(
                'conversion percent must be in (0, 100]; got %r. Write 50 for 50%%, '
                'not 0.5.' % (value,))
        return value


class OperatingCaseRequest(Strict):
    """One operating point the user asked about (e.g. outlet 710 C)."""

    case_id: str
    label: str = ''
    outlet_temperature: float | None = None
    outlet_temperature_unit: str = 'C'
    pressure: float | None = None
    pressure_unit: str = 'kPa'
    thermal_mode: Literal['adiabatic', 'isothermal'] | None = None
    source_text: str = ''


class ProcessRequest(Strict):
    """What the user asked for. Fields may legitimately be absent."""

    source_text: str
    scenario_label: str = ''
    components: list[str] = Field(default_factory=list)
    reactions: list[ReactionSpec] = Field(default_factory=list)
    feeds: list[FeedSpec] = Field(default_factory=list)
    operating_cases: list[OperatingCaseRequest] = Field(default_factory=list)
    kinetic_data: KineticData | None = None
    conversion_constraints: list[ConversionConstraint] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    # Which sentence or rule each extracted field came from, keyed by field path.
    field_sources: dict[str, ValueSource] = Field(default_factory=dict)
    phase: Literal['gas', 'liquid', 'mixed', 'unknown'] = 'unknown'
    # True when a solid component actually takes part in a reaction. Gasification is
    # the case: the feed is coal, which `normalize` represents as solid carbon. The
    # capability table treats a solid-phase Gibbs case as experimental rather than
    # verified, and that only works if the flag reaches it - the scenario's nominal
    # phase is "gas", which on its own made gasification look verified.
    has_solid_reactant: bool = False
    is_polymerisation: bool = False

    def has_kinetics(self) -> bool:
        return self.kinetic_data is not None and self.kinetic_data.is_complete()

    def has_conversion_constraints(self) -> bool:
        return bool(self.conversion_constraints)


# ------------------------------------------------------------ clarification

class Question(Strict):
    """Something we had to ask before this request can be executed.

    `blocking` is the difference between "the run cannot be built without this"
    and "the run is fine but the report must mention it". The tool layer's
    `blocking_questions` behaviour is the model: a blocking question stops
    execution, a non-blocking one becomes a warning.
    """

    id: str
    field: str
    question: str
    blocking: bool = True
    reason: str = ''
    answer: str | None = None

    def is_open(self) -> bool:
        return self.answer is None or not str(self.answer).strip()


class Assumption(Strict):
    """A value we supplied rather than received.

    Anything with `source='agent_default'` must be visible in the final report.
    """

    id: str
    field: str
    value: Any = None
    source: ValueSource
    accepted: bool = False
    scope: str = ''


# --------------------------------------------------------------- the decision

class SelectionDecision(Strict):
    """Which reactor to use, and why - kept apart from whether we can run it.

    `preferred_reactor` is the engineering answer. `execution_reactor` is what the
    tool layer can actually build, or None when nothing can. They differ when the
    right model is not implemented: the plan requires that a PFR selection stays a
    PFR selection reported as UNSUPPORTED, never silently rewritten into
    Conversion to make the run succeed (plan 5.4).
    """

    preferred_reactor: ReactorType
    execution_reactor: ReactorType | None = None
    rule_id: str = ''
    evidence: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    capability_status: CapabilityStatus = 'unsupported'
    fallback_reason: str | None = None
    explanation: str = ''

    def is_executable(self) -> bool:
        return (self.execution_reactor is not None
                and self.execution_reactor != 'unsupported'
                and self.capability_status != 'unsupported')

    def was_substituted(self) -> bool:
        return (self.execution_reactor is not None
                and self.execution_reactor != self.preferred_reactor)


# ------------------------------------------------------------------ planning

class OperatingCase(Strict):
    """One case to run, with its compiled spec and execution bookkeeping."""

    case_id: str
    label: str = ''
    overrides: dict[str, Any] = Field(default_factory=dict)
    spec: dict[str, Any] | None = None
    spec_hash: str | None = None
    attempts: int = 0
    status: str = 'pending'
    result_path: str | None = None
    error: str | None = None

    def compute_spec_hash(self) -> str | None:
        """Stable hash of the spec, for the execution ledger.

        Used to decide whether a resumed run may reuse an existing result: the
        same case with a different spec is a different case (plan 5.5).
        """
        if self.spec is None:
            return None
        payload = json.dumps(self.spec, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def with_spec(self, spec: dict[str, Any]) -> OperatingCase:
        self.spec = spec
        self.spec_hash = self.compute_spec_hash()
        return self


class ModelingPlan(Strict):
    """The modelling decision plus everything needed to execute it."""

    request: ProcessRequest
    decision: SelectionDecision
    components: list[str] = Field(default_factory=list)
    property_package: str = 'PengRob'
    thermal_mode: Literal['adiabatic', 'isothermal'] = 'adiabatic'
    cases: list[OperatingCase] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    status: TaskStatus = 'WAITING_INPUT'

    def blocking_questions(self) -> list[Question]:
        """Open questions that must be answered before anything is executed."""
        return [q for q in self.questions if q.blocking and q.is_open()]

    def is_ready(self) -> bool:
        return not self.blocking_questions() and self.decision.is_executable()

    def default_assumptions(self) -> list[Assumption]:
        return [a for a in self.assumptions if a.source == 'agent_default']
