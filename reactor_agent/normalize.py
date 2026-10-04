"""Deterministic normalisation: raw extracted facts -> a strict ProcessRequest.

This is layer ③ of the four-layer pattern borrowed from GWOA's parser, and it is
pure Python on purpose. The model is asked to copy the user's words verbatim
("380 度", "2.5MPa", "80000Nm3/h"), and everything that requires judgement about
what those words mean happens here, where it can be tested and traced:

  * unit spelling      "度" / "℃" / "摄氏度" -> "C"
  * per-case pressure  `case_pressures` -> the feed pressure, when the request is
                       making one statement about the same stream
  * component names    "甲苯" / "C7H8" -> the HYSYS library name
  * reactions          {reactants, products} -> the signed stoichiometry dict

Two rules this layer must never break:

  * **Do not invent.** A missing value stays missing; it becomes a blocking question.
    Filling it in here would launder a guess into something that looks extracted.
  * **Do not convert what cannot be converted without confirmation.** `Nm3/h` is
    carried through untouched - the tool layer's own pre-check will refuse it - and the
    question about standard conditions and which stream it describes is raised
    explicitly. Once the user confirms a standard state, the total is passed on as the
    tool layer's `normal_volume` input; it is never converted here, and never silently
    to a mass flow.

Every transformation is recorded in a report, so the explanation stage can say what
was changed rather than presenting a normalised value as if the user had written it.
"""
from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

from hysys_tools.core import (
    NORMAL_VOLUME_UNITS,
    atoms_of,
    canonical,
    library_name,
    molar_mass_of,
)

from .extraction import reaction_grounding_failures, reaction_is_derived, written_equations
from .schemas import (
    Assumption,
    ConversionConstraint,
    FeedSpec,
    KineticData,
    OperatingCaseRequest,
    ProcessRequest,
    Question,
    ReactionSpec,
)

# --------------------------------------------------------------------- units

TEMPERATURE_UNITS = {
    'c': 'C', 'degc': 'C', 'celsius': 'C', 'centigrade': 'C', '°c': 'C',
    '℃': 'C', '度': 'C', '摄氏度': 'C', '摄氏': 'C',
    'k': 'K', 'kelvin': 'K', '开尔文': 'K', '°k': 'K',
    'f': 'F', 'degf': 'F', 'fahrenheit': 'F', '华氏度': 'F', '华氏': 'F', '°f': 'F',
}

PRESSURE_UNITS = {
    'pa': 'Pa', 'kpa': 'kPa', 'mpa': 'MPa', 'bar': 'bar', 'mbar': 'mbar',
    'atm': 'atm', 'psi': 'psi',
    '帕': 'Pa', '千帕': 'kPa', '兆帕': 'MPa', '巴': 'bar', '标准大气压': 'atm',
}

FLOW_UNITS = {
    'kg/h': 'kg/h', 'kgh': 'kg/h', 'kg/hr': 'kg/h', '公斤/小时': 'kg/h',
    'kg/s': 'kg/s', 't/h': 't/h', 'ton/h': 't/h', '吨/小时': 't/h',
    'kmol/h': 'kmol/h', 'kmol/hr': 'kmol/h', 'kgmol/h': 'kmol/h',
    'mol/h': 'mol/h', 'mol/s': 'mol/s',
    # Carried through as written. The tool layer refuses volumetric units, and that
    # refusal is the desired behaviour - see the module docstring.
    'nm3/h': 'Nm3/h', 'nm³/h': 'Nm3/h', 'nm3/hr': 'Nm3/h',
    'm3/h': 'm3/h', 'm³/h': 'm3/h',
}

# Components the tool layer models as a solid. A solid that takes part in a reaction
# runs through the accepted saturated-carbon route, which the capability table now
# records as verified.
SOLID_COMPONENTS = {'Carbon'}

# Units the tool layer cannot convert on its own, and why - used to write the
# blocking question. A normal volume is in this set as well, but its question now
# carries a default answer and, once confirmed, the accepted path below.
VOLUMETRIC_UNITS = {'nm3/h', 'nm³/h', 'nm3/hr', 'm3/h', 'm³/h'}

# The spellings `hysys_tools.core.NORMAL_VOLUME_UNITS` accepts, plus the canonical
# spelling this layer rewrites to. Imported from the tool layer rather than copied
# where it matters; this mapping only adds the alias forms that normalise onto it.
_NORMAL_VOLUME_UNITS = frozenset(NORMAL_VOLUME_UNITS) | frozenset(
    {'nm3/h', 'nm³/h', 'nm3/hr', 'nm^3/h', 'nm3h'})

# The standard state offered as the default answer. The tool layer converts a normal
# volume with R*(T+273.15)/P, so 0 C / 101.325 kPa is 22.414 m3/kmol.
DEFAULT_STANDARD_TEMPERATURE_C = 0.0
DEFAULT_STANDARD_PRESSURE_KPA = 101.325

# Hours per year used to turn a chosen molar flow into an annual tonnage. Only ever
# used together with the `a-feed-flow` assumption, which says the user left the figure
# to us.
OPERATING_HOURS_PER_YEAR = 8000


# Chinese and common spellings the model will return, because the user writes Chinese
# and the prompt asks it to copy what it sees. The tool layer maps only canonical
# names, and it is frozen by an accepted real-machine run, so the translation lives
# here rather than in `hysys_tools.core`.
#
# Every target must be a component the tool layer actually supports - there are only
# thirteen, and a test enforces this. Names for species the tool layer cannot model
# (methanol, ethanol, ammonia, H2S, ...) are deliberately absent: mapping them would
# turn "unsupported component" into a later, more confusing failure.
#
# Deliberately incomplete in one more way: "二甲苯" (xylene) has three isomers with
# different HYSYS components, so mapping it to one of them would be a silent, wrong
# choice. It stays unmapped and becomes a question.
SPECIES_ALIASES = {
    '甲苯': 'Toluene',
    '苯': 'Benzene',
    '邻二甲苯': 'o-Xylene', '间二甲苯': 'm-Xylene', '对二甲苯': 'p-Xylene',
    '甲烷': 'Methane', '天然气': 'Methane',
    '水': 'Water', '水蒸气': 'Water', '蒸汽': 'Water', '水蒸汽': 'Water',
    '一氧化碳': 'CO', '二氧化碳': 'CO2',
    '氢气': 'Hydrogen', '氢': 'Hydrogen',
    '氧气': 'Oxygen', '氧': 'Oxygen',
    '氮气': 'Nitrogen', '氮': 'Nitrogen',
    '碳': 'Carbon', '固体碳': 'Carbon', '焦炭': 'Carbon', '煤': 'Carbon',
    '煤炭': 'Carbon', '焦煤': 'Carbon', '石墨': 'Carbon',
}


def resolve_species(name: str) -> str | None:
    """Map a species name onto a HYSYS library name, or None if it cannot be.

    Returns None rather than raising so the caller can raise a question instead of
    dropping the component - an unknown name is information, not a reason to guess.
    """
    text = str(name or '').strip()
    if not text:
        return None
    alias = SPECIES_ALIASES.get(text) or SPECIES_ALIASES.get(text.replace(' ', ''))
    if alias:
        text = alias
    try:
        return library_name(text)
    except Exception:                                   # noqa: BLE001
        return None


def _stable_id(text: str) -> str:
    """A short id that is the same in every process.

    The question ids used to be built from `hash()`, which is salted per process
    (`PYTHONHASHSEED`). A run pauses in one process and is resumed with `--answer` in
    another, so the id the user was shown did not match the id the second process
    looked for and the answer went nowhere. CRC32 is stable across processes.
    """
    return '%08x' % zlib.crc32(str(text).encode('utf-8'))



# Names that stand for several distinct HYSYS components. A model may collapse
# "邻/间/对二甲苯" into "二甲苯" or "C8H10" inside a reaction even when it listed the
# isomers separately, and the exam for scenario 2 names all three without giving a
# ratio. Expanding them equally is the only defensible reading, but it IS an
# assumption, so it is recorded as one rather than presented as extracted.
_ISOMER_GROUPS: dict[str, tuple[str, ...]] = {
    '二甲苯': ('o-Xylene', 'm-Xylene', 'p-Xylene'),
    'xylene': ('o-Xylene', 'm-Xylene', 'p-Xylene'),
    'c8h10': ('o-Xylene', 'm-Xylene', 'p-Xylene'),
}

_SUBSCRIPT_DIGITS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')


def _fold_formula(text: str) -> str:
    """'C₈H₁₀' -> 'c8h10' so a formula matches the group table."""
    folded = str(text or '').strip().translate(_SUBSCRIPT_DIGITS)
    return folded.replace(' ', '').casefold()


def expand_ambiguous_species(name: str, present: list[str]) -> tuple[list[str], bool]:
    """Expand a group name such as 二甲苯 into its members, when they are present.

    Returns (species, expanded). Only expands when every member is already among the
    request's components, so this cannot introduce a species the user never mentioned.
    """
    text = str(name or '').strip()
    members = _ISOMER_GROUPS.get(text) or _ISOMER_GROUPS.get(_fold_formula(text))
    if not members:
        return [], False
    if all(member in present for member in members):
        return list(members), True
    return [], False


def stated_xylene_isomers(text: str) -> bool:
    return (all(name in text for name in ('邻二甲苯', '间二甲苯', '对二甲苯'))
            or bool(re.search(r'邻\s*[、,/和及]\s*间\s*[、,/和及]\s*对\s*(?:三种)?二甲苯', text)))


def _canonical_unit(raw: Any, table: dict[str, str], default: str) -> tuple[str, bool]:
    """Return (canonical unit, changed?)."""
    text = str(raw or '').strip()
    if not text:
        return default, False
    key = text.casefold().replace(' ', '').replace('°', '°')
    canonical = table.get(key)
    if canonical is None:
        return text, False
    return canonical, canonical != text


# How the user expressed the feed composition, and what that means for the spec.
# "mole_ratio" is the reforming case ("摩尔比 1:2.7"): a proportion, not a fraction,
# so it must be normalised to sum to 1.
COMPOSITION_BASES = {
    'mass_fraction': 'mass_fraction', 'mass_percent': 'mass_fraction',
    'weight_fraction': 'mass_fraction', 'wt': 'mass_fraction', 'wt%': 'mass_fraction',
    'mole_ratio': 'molar_fraction', 'mol_ratio': 'molar_fraction',
    'molar_ratio': 'molar_fraction', 'molar_fraction': 'molar_fraction',
    'mole_fraction': 'molar_fraction', 'mol_fraction': 'molar_fraction',
    'pure': 'molar_fraction', 'single': 'molar_fraction',
}


def _slurry_composition_default(source_text: str) -> dict[str, Any] | None:
    """A narrowly stated binary coal-water slurry, never a guessed coal analysis.

    Its concentration fixes the coal/water mass split; representing the coal as
    Carbon still needs the separate compiler confirmation. Multiple concentrations
    or explicitly named additives must be clarified instead of collapsed.
    """
    if any(word in source_text for word in ('添加', '助剂', '添加剂', '多股', '两股')):
        return None
    matches = re.findall(
        r'水煤浆\s*(?:进料)?\s*(?:质量)?浓度\s*(?:为|是|[:：=])?\s*'
        r'(\d+(?:\.\d+)?)\s*(?:wt\s*%|质量百分比|质量%)',
        source_text, flags=re.IGNORECASE)
    if len(matches) != 1 or not 0 < float(matches[0]) < 100:
        return None
    coal = float(matches[0])
    return {'composition': [{'name': '煤炭', 'fraction': coal},
                            {'name': '水', 'fraction': 100 - coal}],
            'basis': 'mass_fraction'}


def _feed_composition(facts: dict[str, Any], report: 'NormalizationReport',
                      fallback_basis: str,
                      source_text: str = '') -> tuple[str, dict[str, float]]:
    """Return (spec basis, fractions summing to 1).

    The model is told to copy numbers as written, so a 1:2.7 molar ratio arrives as
    1.0 and 2.7, and a 62 wt% slurry as 62 and 38. Normalising those is arithmetic,
    and it belongs here rather than in the prompt.

    When the user gave no composition at all, the basis falls back to what the caller
    said the scenario is - but no fractions are invented, and the missing composition
    becomes a question.
    """
    entries = facts.get('feed_composition') or []
    stated_basis = str(facts.get('composition_basis') or '').strip().casefold()
    basis = COMPOSITION_BASES.get(stated_basis, fallback_basis)

    raw: dict[str, float] = {}
    unknown: list[str] = []
    for entry in entries:
        name = resolve_species(entry.get('name'))
        if name is None:
            unknown.append(str(entry.get('name')))
            continue
        value = entry.get('fraction')
        raw[name] = float(value) if value is not None else 1.0

    default = _slurry_composition_default(source_text)
    total = sum(abs(value) for value in raw.values())
    slurry_mismatch = default is not None and (
        basis != 'mass_fraction' or set(raw) != {'Carbon', 'Water'} or
        total <= 0 or abs(raw.get('Carbon', 0) / total -
                          default['composition'][0]['fraction'] / 100) > 1e-6)
    if unknown or slurry_mismatch:
        reason = ('组成中的名称无法解析：%s。' % '、'.join(unknown)) if unknown else ''
        if slurry_mismatch:
            reason += '水煤浆浓度描述煤的质量百分比；提取的组成或基准与原文不符。'
        report.questions.append(Question(
            id='q-feed-composition', field='feeds[0].fractions', blocking=True,
            question='请确认完整的进料组成：逐个给出标准组分名称、比例及质量或摩尔基准。',
            reason=reason, default=(json.dumps(default, ensure_ascii=False)
                                    if default else None)))
        # Never compile the surviving subset of an unreadable mixture, or an
        # inverted concentration, as though it were a complete feed.
        return basis, {}

    if not raw:
        return basis, {}

    if len(raw) == 1:
        # A single-substance feed is fully specified by its name.
        only = next(iter(raw))
        report.record('feed composition: single component %r -> fraction 1.0' % only)
        return basis, {only: 1.0}

    total = sum(abs(value) for value in raw.values())
    if total <= 0:
        report.questions.append(Question(
            id='q-composition-zero', field='feeds[0].fractions', blocking=True,
            question='进料组成的数值全为 0，请确认各组分的比例。',
            reason='组成之和为零，无法建立进料。'))
        return basis, {}

    fractions = {name: abs(value) / total for name, value in raw.items()}
    if stated_basis in ('mole_ratio', 'mol_ratio', 'molar_ratio'):
        report.record('molar ratio %s normalised to mole fractions %s'
                      % ({k: round(v, 6) for k, v in raw.items()},
                         {k: round(v, 6) for k, v in fractions.items()}))
    elif stated_basis in ('mass_percent', 'mass_fraction', 'wt%'):
        report.record('mass percentages %s normalised to mass fractions %s'
                      % ({k: round(v, 6) for k, v in raw.items()},
                         {k: round(v, 6) for k, v in fractions.items()}))
    return basis, fractions


# ------------------------------------------------------------------- reactions

def _stoichiometry(reaction: dict, present: list[str],
                   report: 'NormalizationReport', source_text: str = '') -> dict[str, float]:
    """Signed coefficients from the model's single signed species list.

    The model is asked for one list where a negative coefficient is consumed and a
    positive one is produced (see `extraction._reaction_entry` for why one list and
    not two). The signs are therefore taken as given rather than inferred from which
    array an entry appeared in.

    A coefficient of exactly zero is dropped: it contributes nothing and would only
    clutter the spec.

    Names are resolved here as well as in the component list, so the stoichiometry
    keys and `fluid_package.components` always agree. An unresolvable name becomes a
    blocking question rather than a silent omission, and a name that stands for
    several components (xylene, or a formula like C8H10) is expanded into its members
    **only when all of them are already in the request** - the equal split is then
    recorded as an assumption, because the exam names the three isomers without
    giving a ratio.
    """
    result: dict[str, float] = {}

    def add(raw_name: Any, coefficient: float) -> None:
        if abs(coefficient) < 1e-12:
            return
        species = resolve_species(raw_name)
        if species:
            if species not in present:
                # A species that appears only in a reaction still has to be in the
                # fluid package, or the reaction cannot be built.
                present.append(species)
                report.record('component %r added from a reaction' % species)
            result[species] = result.get(species, 0.0) + coefficient
            return
        members, expanded = expand_ambiguous_species(raw_name, present)
        if not expanded:
            report.questions.append(Question(
                id='q-reaction-species-%s' % _stable_id(str(raw_name)),
                field='reactions', blocking=True,
                question='反应式中的组分 %r 无法对应到 HYSYS 库组分，请确认它的标准名称。'
                         % str(raw_name),
                reason='反应物或产物不能静默丢弃，否则反应式与题目不符。'))
            return
        each = coefficient / len(members)
        for member in members:
            result[member] = result.get(member, 0.0) + each
            if member not in report.expanded_species:
                report.expanded_species.append(member)
        report.record(
            '%r expanded to %s, split equally (%s)'
            % (str(raw_name), ', '.join(members),
               'user-stated equal molar ratio' if _equal_xylene_split_stated(source_text)
               else 'no stated ratio; recorded as an assumption'))

    for entry in (reaction.get('species') or []):
        try:
            coefficient = float(entry.get('coefficient') or 0.0)
        except (TypeError, ValueError):
            coefficient = 0.0
        add(entry.get('name'), coefficient)

    cleaned = {name: value for name, value in result.items() if abs(value) > 1e-12}
    _restore_isomer_group_total(cleaned, source_text, report)

    # A reaction needs both directions to mean anything. Reporting this here, as a
    # question, is far more useful than letting the tool layer reject a spec whose
    # elements do not balance.
    if cleaned and not any(value > 0 for value in cleaned.values()):
        report.questions.append(Question(
            id='q-reaction-no-product', field='reactions', blocking=True,
            question='反应式 %r 只有反应物、没有产物，请重新给出完整的反应式。'
                     % str(reaction.get('name') or ''),
            reason='没有产物就无法确定反应方向；把产物也放进同一个列表时，'
                   '产物的系数必须为正。'))
    if cleaned and not any(value < 0 for value in cleaned.values()):
        report.questions.append(Question(
            id='q-reaction-no-reactant', field='reactions', blocking=True,
            question='反应式 %r 只有产物、没有反应物，请重新给出完整的反应式。'
                     % str(reaction.get('name') or ''),
            reason='没有反应物就无法确定反应方向。'))
    return cleaned


def _restore_isomer_group_total(stoich: dict[str, float], source_text: str,
                               report: 'NormalizationReport') -> None:
    """Split a written group coefficient, never multiply it by the isomer count.

    Only repair equal isomer coefficients when a unique, balanced written equation
    specifies the group total and agrees with every other species and coefficient.
    Unknown chemistry, uneven selectivity and other coefficient errors stay subject
    to the normal pre-check; this is not a general reaction balancer.
    """
    isomers = ('o-Xylene', 'm-Xylene', 'p-Xylene')
    values = [stoich.get(name) for name in isomers]
    if any(value is None or abs(value) < 1e-12 for value in values):
        return
    if max(values) - min(values) > 1e-9:
        return
    candidates = []
    for equation in written_equations(source_text):
        total = equation.get('C8H10')
        if total is None or total * values[0] <= 0:
            continue
        other = {resolve_species(formula): value for formula, value in equation.items()
                 if formula != 'C8H10'}
        if None in other or set(stoich) != set(other) | set(isomers):
            continue
        if any(abs(stoich[name] - value) > 1e-9 for name, value in other.items()):
            continue
        corrected = dict(other, **{name: total / 3 for name in isomers})
        net: dict[str, float] = {}
        for name, coefficient in corrected.items():
            for element, count in atoms_of(name).items():
                net[element] = net.get(element, 0) + coefficient * count
        if all(abs(value) <= 1e-9 for value in net.values()):
            candidates.append(total)
    if len(candidates) != 1:
        return
    total = candidates[0]
    if abs(sum(values) - total) <= 1e-9:
        return
    for name in isomers:
        stoich[name] = total / 3
        if name not in report.expanded_species:
            report.expanded_species.append(name)
    report.record('xylene group total restored from the written equation: %g -> %g; '
                  'o/m/p coefficients %g each (%s)'
                  % (sum(values), total, total / 3,
                     'user-stated equal molar ratio' if _equal_xylene_split_stated(source_text)
                     else 'equal split recorded as an assumption'))


def _kinetic_data(facts: dict[str, Any], source_text: str) -> 'KineticData | None':
    """Build the rate-based data, or None when the request stated none.

    Without this the information was dropped at extraction and `has_kinetics()` was
    permanently False, so a request that gave a full rate law and a reactor volume was
    read as "no kinetics provided" and quietly modelled as a Conversion reactor. That
    is the silent substitution this project is supposed to refuse.
    """
    rate_law = str(facts.get('rate_law') or '').strip()
    pre_exponential = facts.get('pre_exponential')
    activation = facts.get('activation_energy')
    volume = facts.get('reactor_volume')
    residence = facts.get('residence_time')
    catalyst = facts.get('catalyst_mass')
    orders = {str(entry.get('name')): float(entry.get('order') or 0.0)
              for entry in (facts.get('reaction_order') or [])
              if entry.get('name') is not None}
    if not any((rate_law, pre_exponential, activation, volume, residence, catalyst,
                orders)):
        return None

    def as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    energy = as_float(activation)
    energy_unit = str(facts.get('activation_energy_unit') or '').strip().casefold()
    if energy is not None:
        # J/mol is the contract's unit; kJ/mol is what a person writes.
        if 'kj' in energy_unit or (not energy_unit and energy < 1000):
            energy *= 1000.0

    volume_m3 = as_float(volume)
    volume_unit = str(facts.get('reactor_volume_unit') or '').strip().casefold()
    if volume_m3 is not None and volume_unit in ('l', 'l/h', '升', 'liter', 'litre'):
        volume_m3 /= 1000.0

    residence_s = as_float(residence)
    residence_unit = str(facts.get('residence_time_unit') or '').strip().casefold()
    if residence_s is not None and residence_unit in ('min', '分钟', '分'):
        residence_s *= 60.0
    elif residence_s is not None and residence_unit in ('h', 'hr', 'hour', '小时'):
        residence_s *= 3600.0

    return KineticData(
        rate_law=rate_law,
        pre_exponential=as_float(pre_exponential),
        activation_energy_J_mol=energy,
        reaction_order=orders,
        reactor_volume_m3=volume_m3,
        catalyst_mass_kg=as_float(catalyst),
        residence_time_s=residence_s,
        source_text=source_text[:200])


# --------------------------------------------------------------------- report

@dataclass
class NormalizationReport:
    """What normalisation changed, and what it could not resolve."""
    applied: list[str] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    # Species introduced by expanding an ambiguous group (xylene -> o/m/p) rather
    # than by resolving a name. Per call, not module-level: shared mutable state here
    # would leak between runs and make one request's assumptions exempt another's.
    expanded_species: list[str] = field(default_factory=list)

    def record(self, message: str) -> None:
        self.applied.append(message)

    @property
    def blocking(self) -> list[Question]:
        return [q for q in self.questions if q.blocking]

    @property
    def agent_choices(self) -> list[Assumption]:
        """Assumptions this agent made rather than received; they must be reported."""
        return [a for a in self.assumptions if a.source == 'agent_default']


# Phrases that hand a decision to us instead of stating it. Scenario 1 says the
# reformer feed flow "可以自定，要求符合一个工厂一年正常的处理量" - that is an
# instruction to choose a sensible value, not a missing fact, so treating it as a
# blocking question would stop a run the user explicitly authorised.
_CHOICE_MARKERS = (
    '可以自定', '可自定', '自定', '自行设定', '由你决定', '你决定', '任意',
    '自行选择', '你来定', '自行确定',
    'may choose', 'at your discretion', 'up to you', 'choose a sensible',
)

# Industrial scale for a mid-size plant, used only when the user has delegated the
# flow to us. A thousand kmol/h of the principal reactant is ~16 t/h of methane,
# which is the order of a mid-size steam-reforming unit.
DEFAULT_PRINCIPAL_KMOL_H = 1000.0


def flow_is_ours_to_choose(source_text: str) -> bool:
    """True when the request delegates the feed flow to us rather than stating it."""
    text = (source_text or '').casefold()
    return any(marker in text for marker in _CHOICE_MARKERS)


def _flow_anchor(fractions: dict[str, float],
                 reactions: list[ReactionSpec]) -> str:
    """Which feed component the delegated total flow should be anchored on.

    Anchoring on the largest fraction picked water for the reformer (1:2.7), which
    then gave methane only 370 kmol/h instead of the 1000 kmol/h the exam intends.
    The anchor has to be the carbon-bearing reactant the process is named after.
    """
    def contains_carbon(name: str) -> bool:
        try:
            return 'C' in atoms_of(name)
        except Exception:                               # noqa: BLE001
            return False

    consumed = {canonical(name)
                for reaction in reactions
                for name, coefficient in reaction.stoichiometry.items()
                if coefficient < 0}

    carbon_reactants = [name for name in fractions
                        if canonical(name) in consumed and contains_carbon(name)]
    if carbon_reactants:
        return max(carbon_reactants, key=lambda name: fractions[name])

    if not reactions:
        carbon_feed = [name for name in fractions if contains_carbon(name)]
        if carbon_feed:
            return max(carbon_feed, key=lambda name: fractions[name])

    return max(fractions, key=lambda name: fractions[name])


# The three xylene isomers, as the exam names them.
_XYLENE_ISOMERS = ('o-Xylene', 'm-Xylene', 'p-Xylene')


def _equal_xylene_split_stated(text: str) -> bool:
    for clause in re.split(r'[。；;，,\n]', text):
        if not re.search(r'二甲苯|xylene', clause, re.I):
            continue
        if re.search(r'(?:不|非|未)[^。；;\n]{0,6}等摩尔', clause):
            continue
        if re.search(r'等摩尔|equal\s+molar|1\s*[:：]\s*1\s*[:：]\s*1', clause, re.I):
            return True
    return False


def _isomer_split_applies(reactions: list[ReactionSpec]) -> bool:
    """True when the request treats the three xylene isomers as an equal split.

    Two routes lead here, and both need the same declaration:

    * `_stoichiometry` expanded the group name "二甲苯" / "C8H10" into three isomers;
    * the model itself already wrote the three isomers with equal coefficients. That
      is what the smoke record shows it doing, and the earlier code recorded nothing
      in that case, so the equal split reached the spec undeclared.
    """
    for reaction in reactions:
        coefficients = [reaction.stoichiometry.get(member)
                        for member in _XYLENE_ISOMERS]
        if any(value is None for value in coefficients):
            continue
        values = [abs(float(value)) for value in coefficients]
        if all(value > 0 for value in values) and max(values) - min(values) <= 1e-9:
            return True
    return False




# ------------------------------------------------------------------ the layer

def normalize(facts: dict[str, Any], source_text: str, *,
              scenario_label: str = '', phase: str = 'unknown',
              feed_basis: str | None = None,
              ungrounded: list[str] | None = None
              ) -> tuple[ProcessRequest, NormalizationReport]:
    """Turn extracted facts into a ProcessRequest. Never invents, never converts.

    `feed_basis` lets the caller state how the composition was given (molar or mass);
    the intake stage knows this from the scenario, and guessing it here would change
    every downstream number.

    `ungrounded` names fields whose value was not found in the user's own words. They
    become BLOCKING questions. Reporting them without blocking would be worse than
    useless: the fabricated value is still present in `facts`, so it would flow
    straight into a case and produce a confident wrong answer.
    """
    report = NormalizationReport()
    from .input_recovery import recover_explicit_units
    facts, unit_records = recover_explicit_units(source_text, facts)
    for item in unit_records:
        report.record('unit recovered from the original request: %s = %s' % (item['field'], item['value']))
    for item in facts.get('_review_questions') or []:
        path = {'feed_total': 'feeds[0].total_flow',
                'feed_temperature': 'feeds[0].temperature',
                'feed_pressure': 'feeds[0].pressure',
                'feed_composition': 'feeds[0].fractions'}.get(item['field'], item['field'])
        report.questions.append(Question(id='q-review:' + item['field'],
            field=path, blocking=True, question=item['question'], reason=item['reason']))

    for field in ungrounded or []:
        # The id carries the field name verbatim after a colon, so `_apply_answers`
        # can route an answer straight back to the field it belongs to instead of
        # guessing. An unparseable id is the one thing that would make an answer
        # silently go nowhere.
        unit_hint = {'feed_total': '（请连同单位一起给出，例如 10000 kg/h）',
                     'feed_pressure': '（请连同单位一起给出）',
                     'feed_temperature': '（请连同单位一起给出）',
                     }.get(field, '')
        report.questions.append(Question(
            id='q-ungrounded:%s' % field,
            field=field, blocking=True,
            question='%s 的取值在您的描述中找不到依据，模型给出的值可能是编造的，'
                     '请确认正确的数值。%s' % (field, unit_hint),
            reason='防幻觉检查：抽取出的每个数值都必须能在原始描述中找到。'
                   '未经确认的数值不会用于建模。'))

    # ---------------------------------------------------------------- feeds
    for value_field, unit_field, label in (
            ('feed_total', 'feed_unit', '进料流量'),
            ('feed_temperature', 'feed_temperature_unit', '进料温度'),
            ('feed_pressure', 'feed_pressure_unit', '进料压力')):
        if (facts.get(value_field) is not None and not str(facts.get(unit_field) or '').strip()
                and not any(q.field == value_field for q in report.questions)):
            report.questions.append(Question(id='q-review:' + unit_field, field=unit_field,
                blocking=True, question='请提供%s的单位。' % label,
                reason='已知数值不能套用默认单位；先查原文，原文未给出时需要确认。'))
    temperature, changed = _canonical_unit(facts.get('feed_temperature_unit'),
                                           TEMPERATURE_UNITS, 'C')
    if changed:
        report.record('feed temperature unit %r -> %r'
                      % (facts.get('feed_temperature_unit'), temperature))

    pressure_unit, changed = _canonical_unit(facts.get('feed_pressure_unit'),
                                             PRESSURE_UNITS, 'kPa')
    if changed:
        report.record('feed pressure unit %r -> %r'
                      % (facts.get('feed_pressure_unit'), pressure_unit))

    flow_unit, changed = _canonical_unit(facts.get('feed_unit'), FLOW_UNITS, 'kg/h')
    if changed:
        report.record('feed flow unit %r -> %r' % (facts.get('feed_unit'), flow_unit))

    pressure = facts.get('feed_pressure')
    case_pressures = [p for p in (facts.get('case_pressures') or []) if p is not None]
    # A zero pressure drop explicitly connects each case's inlet and outlet.
    per_case_feed = bool(case_pressures and re.search(
        r'(?<!非)(?<!不)(?:无压降|没有压降)|压降\s*(?:为|=|是)\s*0'
        r'|no\s+pressure\s+drop|进料和出口压力均为', source_text, re.I))
    if pressure is None and case_pressures:
        # The request stated one pressure per operating case; when every case agrees,
        # that is the same statement as a feed pressure and can be used as one.
        if len(set(case_pressures)) == 1:
            pressure = case_pressures[0]
            case_unit, changed = _canonical_unit(facts.get('case_pressure_unit'),
                                                 PRESSURE_UNITS, pressure_unit)
            if changed:
                report.record('case pressure unit %r -> %r'
                              % (facts.get('case_pressure_unit'), case_unit))
            pressure_unit = case_unit or pressure_unit
            report.record('feed pressure taken from the per-case pressure (all cases '
                          'state %g %s)' % (float(pressure), pressure_unit))
        elif not per_case_feed:
            report.questions.append(Question(
                id='q-pressure-varies', field='feeds[0].pressure', blocking=True,
                question='各工况的压力不同（%s），请确认进料压力按哪一个？'
                         % ', '.join('%g' % p for p in case_pressures),
                reason='题面只给出工况压力，且各工况不一致，无法确定进料压力。'))

    total_flow = facts.get('feed_total')
    # The normal-volume basis is written by the answer router once the user confirms
    # the stream and the standard state. Until then nothing is assumed, and nothing is
    # converted either way.
    normal_basis = facts.get('normal_volume_basis') or {}
    normal_basis_temperature = normal_basis.get('standard_temperature_C')
    normal_basis_pressure = normal_basis.get('standard_pressure_kPa')
    normal_basis_given = (normal_basis_temperature is not None
                          and normal_basis_pressure is not None)

    if (total_flow is not None and normal_basis_given
            and str(flow_unit).casefold() in _NORMAL_VOLUME_UNITS):
        if flow_unit != 'Nm3/h':
            report.record('normal volume unit %r -> %r' % (flow_unit, 'Nm3/h'))
            flow_unit = 'Nm3/h'
        report.record(
            'normal volume confirmed by the user: %g %s at %g C / %g kPa; carried as '
            'flow_input=normal_volume, not converted here'
            % (float(total_flow), flow_unit, float(normal_basis_temperature),
               float(normal_basis_pressure)))
        report.assumptions.append(Assumption(
            id='a-normal-volume', field='feeds[0].total_flow', source='user_answer',
            accepted=True,
            value='%g %s @ %g°C/%g kPa'
                  % (float(total_flow), flow_unit,
                     float(normal_basis_temperature),
                     float(normal_basis_pressure)),
            scope=('这是单股混合进料的总量；按理想气体摩尔体积换算，'
                   '0°C/101.325 kPa 时为 22.414 m³/kmol，即 R(T+273.15)/P；'
                   '由工具层换算成摩尔流量交给 HYSYS，'
                   '不用 HYSYS 自带的 15°C 标准体积')))
    elif total_flow is not None and str(flow_unit).casefold() in VOLUMETRIC_UNITS:
        # Raised as a blocking question rather than converted; see the docstring.
        # A standard gas volume gets a default answer, because there is one reading
        # the tool layer has accepted and the user only has to confirm it.
        normal_volume_unit = str(flow_unit).casefold() in _NORMAL_VOLUME_UNITS
        if normal_volume_unit:
            question_text = (
                '进料流量 %g %s 按什么理解？默认：单股混合进料的总量，'
                '标准状态 0°C / 101.325 kPa。也可以回答其他标准状态（如 20°C），'
                '或直接给出质量或摩尔流量（如 49086 kg/h）。'
                % (float(total_flow), flow_unit))
        else:
            question_text = (
                '进料流量 %g %s 指的是哪一股物流？请直接给出质量或摩尔流量。'
                % (float(total_flow), flow_unit))
        report.questions.append(Question(
            id='q-volumetric-flow', field='feeds[0].total_flow_unit', blocking=True,
            question=question_text,
            default=('总进料，0°C/101.325 kPa' if normal_volume_unit else None),
            reason=('体积单位无法换算成摩尔或质量流量：Nm³ 只对气体有意义，而煤是'
                    '固体、水是液体。不同解释会给出完全不同的 CO 收率，因此不能自行'
                    '选定一种。')))
        report.record('volumetric flow %g %s kept verbatim, NOT converted'
                      % (float(total_flow), flow_unit))

    if facts.get('feed_temperature') is None:
        report.questions.append(Question(
            id='q-feed-temperature', field='feeds[0].temperature', blocking=True,
            question='进料温度是多少？', reason='进料温度是物料与能量衡算的必需条件。'))
    if pressure is None and not per_case_feed and not any(
            q.field == 'feeds[0].pressure' for q in report.questions):
        report.questions.append(Question(
            id='q-feed-pressure', field='feeds[0].pressure', blocking=True,
            question='操作压力是多少？', reason='压力是物料与能量衡算的必需条件。'))
    # `q-feed-flow` is decided below, once we know whether the user delegated the
    # flow to us - asking for a value they explicitly left to us would be wrong.

    # ------------------------------------------------------------ components
    # Only the species list defines the fluid package here. Names inside reactions
    # are handled by `_stoichiometry`, which either resolves them, expands an
    # ambiguous group, or raises a question - so a name like "C8H10" is not reported
    # twice, once as a component and once as a reaction species.
    raw_species = [str(s).strip() for s in (facts.get('species') or []) if str(s).strip()]
    components: list[str] = []
    for name in raw_species:
        resolved = resolve_species(name)
        if resolved is None:
            if _fold_formula(name) in _ISOMER_GROUPS and stated_xylene_isomers(source_text):
                for member in _ISOMER_GROUPS[_fold_formula(name)]:
                    if member not in components:
                        components.append(member)
                report.record('component group %r expanded to the three isomers stated in the request' % name)
                continue
            # An unknown spelling is a question, not something to drop silently.
            report.questions.append(Question(
                id='q-component-%s' % _stable_id(name),
                field='fluid_package.components', blocking=True,
                question='组分 %r 无法对应到 HYSYS 库组分，请确认它的标准名称。' % name,
                reason='未知组分不能静默丢弃，否则物性包会缺少必要的组分；'
                       '也不能自行猜一个相近的组分。'))
            continue
        if resolved not in components:
            components.append(resolved)
            if resolved != name:
                report.record('component %r -> %r' % (name, resolved))

    # ------------------------------------------------------------- reactions
    reactions: list[ReactionSpec] = []
    for index, reaction in enumerate(facts.get('reactions') or []):
        # `components` is already resolved, so the expander can check membership.
        stoich = _stoichiometry(reaction, components, report, source_text)
        if not stoich:
            continue
        reactions.append(ReactionSpec(
            name=str(reaction.get('name') or 'RXN-%d' % (index + 1)),
            stoichiometry=stoich,
            reversible=reaction.get('reversible'),
            source_text=source_text[:200]))

    # Coefficients must be traceable too, but a coefficient that came from a recorded
    # assumption (the equal xylene split) is exempt: it is declared, so it is the
    # user's to challenge rather than a silent invention.
    assumed_species = set(report.expanded_species)
    for field in reaction_grounding_failures(facts.get('reactions') or [], source_text,
                                             assumed=assumed_species):
        report.questions.append(Question(
            id='q-ungrounded:%s' % field, field=field, blocking=True,
            question='反应系数 %s 在您的描述中找不到依据，请确认。' % field,
            reason='防幻觉检查：用户给出了带数字的方程式时，反应式中的系数必须'
                   '来自该方程式。'))

    # A reaction the request worded rather than wrote leaves the coefficients to be
    # derived from the element balance. That is chemistry, not invention, so it is not
    # blocked - but it is our derivation, and it is declared as one.
    for reaction in (facts.get('reactions') or []):
        if reactions and reaction_is_derived(reaction, source_text):
            report.assumptions.append(Assumption(
                id='a-stoichiometry-%s' % (reaction.get('name') or 'rxn'),
                field='reactions[%s].stoichiometry' % (reaction.get('name') or 'rxn'),
                value='derived', source='agent_default', accepted=False,
                scope='题目用文字描述该反应、未给出系数，因此配平系数由元素守恒推导'
                      '（工具层会独立校验元素守恒）'))

    # ------------------------------------------------------------ conversion
    constraints: list[ConversionConstraint] = []
    percent = facts.get('conversion_percent')
    if percent is not None:
        basis = str(facts.get('conversion_basis') or '').strip()
        if basis:
            # Resolve so the basis matches the stoichiometry keys.
            basis = resolve_species(basis) or basis
        if not basis and len(reactions) == 1:
            reactants = [n for n, v in reactions[0].stoichiometry.items() if v < 0]
            if len(reactants) == 1:
                basis = reactants[0]
                report.record('conversion basis inferred from the only reactant: %r'
                              % basis)
        if not basis:
            report.questions.append(Question(
                id='q-conversion-basis', field='conversion_percent', blocking=True,
                question='转化率是针对哪个反应物（基准组分）？',
                reason='转化率必须相对一个基准组分定义，否则无法确定反应进度。'))
        constraints.append(ConversionConstraint(
            reaction=reactions[0].name if len(reactions) == 1 else '',
            percent=float(percent), base_component=basis, source_text=source_text[:200]))

    # ----------------------------------------------------------------- cases
    outlet_temperature, _ = _canonical_unit(facts.get('outlet_temperature_unit'),
                                            TEMPERATURE_UNITS, temperature)
    cases: list[OperatingCaseRequest] = []
    temperatures = facts.get('outlet_temperatures') or []
    case_pressure_unit = _canonical_unit(facts.get('case_pressure_unit'),
                                          PRESSURE_UNITS, pressure_unit)[0]
    if case_pressures and len(case_pressures) not in (1, len(temperatures)):
        report.questions.append(Question(id='q-review:outlet_temperatures',
            field='outlet_temperatures', blocking=True,
            question='请按工况顺序给出每个出口温度的 JSON 列表，相同温度也需重复列出。',
            reason='工况压力与出口温度的数量不一致，不能猜测对应关系。'))
    for index, value in enumerate(temperatures):
        if value is None:
            continue
        case_pressure = pressure
        if case_pressures and (len(case_pressures) == 1 or index < len(case_pressures)):
            case_pressure = case_pressures[0 if len(case_pressures) == 1 else index]
        cases.append(OperatingCaseRequest(
            case_id='case-%d' % (index + 1), label='%g %s' % (float(value),
                                                              outlet_temperature),
            outlet_temperature=float(value),
            outlet_temperature_unit=outlet_temperature,
            pressure=case_pressure,
            pressure_unit=case_pressure_unit if case_pressures else pressure_unit,
            feed_pressure=case_pressure if per_case_feed else None,
            feed_pressure_unit=case_pressure_unit,
            thermal_mode='isothermal', source_text=source_text[:200]))

    composition_basis, fractions = _feed_composition(
        facts, report, feed_basis or 'molar_fraction', source_text)

    if total_flow is None and fractions and flow_is_ours_to_choose(source_text):
        # The user delegated the flow. Choose one at a defensible scale, anchored on
        # the carbon-bearing reactant so the stated ratio is preserved, and record it
        # as an assumption - never as if it had been extracted.
        principal = _flow_anchor(fractions, reactions)
        share = fractions[principal]
        total_flow = DEFAULT_PRINCIPAL_KMOL_H / share
        flow_unit = 'kmol/h'
        try:
            annual_kt = (DEFAULT_PRINCIPAL_KMOL_H * molar_mass_of(principal)
                         * OPERATING_HOURS_PER_YEAR / 1e6)
        except Exception:                               # noqa: BLE001
            annual_kt = None
        if annual_kt is None:
            scope = ('题目说明进料流量可以自定、未给数值；取 %s %g kmol/h'
                     '（总进料 %g kmol/h，保持原有比例），属于中型装置的处理量'
                     % (principal, DEFAULT_PRINCIPAL_KMOL_H, round(total_flow, 6)))
        else:
            scope = ('题目说明进料流量可以自定、未给数值；取 %s %g kmol/h'
                     '（总进料 %g kmol/h，保持原有比例），按 %d h/a 计约 %.1f kt/a %s，'
                     '属于中型装置的处理量'
                     % (principal, DEFAULT_PRINCIPAL_KMOL_H, round(total_flow, 6),
                        OPERATING_HOURS_PER_YEAR, annual_kt, principal))
        report.assumptions.append(Assumption(
            id='a-feed-flow', field='feeds[0].total_flow', value=round(total_flow, 6),
            source='agent_default', accepted=False, scope=scope))
        report.record('feed flow chosen by us: %g kmol/h (%s at %g kmol/h), recorded '
                      'as an assumption' % (round(total_flow, 6), principal,
                                            DEFAULT_PRINCIPAL_KMOL_H))

    if not fractions and not any(q.field == 'feeds[0].fractions'
                                 for q in report.questions):
        report.questions.append(Question(
            id='q-feed-composition', field='feeds[0].fractions', blocking=True,
            question='进料组成（各组分及其比例）是什么？',
            reason='没有进料组成就无法建立物料衡算；不能自行假定一个组成。'))

    flow_was_our_choice = any(a.field == 'feeds[0].total_flow'
                              for a in report.assumptions)
    if total_flow is None and not flow_was_our_choice:
        # Not delegated, and nothing was chosen: this really is missing. The check is
        # for the FLOW assumption specifically - testing `report.assumptions` at all
        # meant any other assumption (a derived stoichiometry, say) silently suppressed
        # the question about a missing flow.
        report.questions.append(Question(
            id='q-feed-flow', field='feeds[0].total_flow', blocking=True,
            question='进料流量是多少？请给出数值与单位。',
            reason='没有流量就无法建立物料衡算。'))

    feed = FeedSpec(
        basis=composition_basis,                    # type: ignore[arg-type]
        fractions=fractions,
        total_flow=total_flow, total_flow_unit=flow_unit,
        temperature=facts.get('feed_temperature'), temperature_unit=temperature,
        pressure=pressure, pressure_unit=pressure_unit,
        # Only the accepted normal-volume path writes these; a local flow keeps them
        # at their defaults so the spec is unchanged.
        flow_input=('normal_volume'
                    if normal_basis_given
                    and str(flow_unit).casefold() in _NORMAL_VOLUME_UNITS
                    else 'local'),
        standard_temperature_C=(float(normal_basis_temperature)
                                if normal_basis_given else None),
        standard_pressure_kPa=(float(normal_basis_pressure)
                               if normal_basis_given else None),
        source_text=source_text[:200])

    # ------------------------------------------------------------- kinetics
    kinetics = _kinetic_data(facts, source_text)
    if kinetics is not None:
        report.record('kinetic data recorded: rate law %r, volume %s m3'
                      % (kinetics.rate_law[:40] or '(from parameters)',
                         kinetics.reactor_volume_m3))

    # The phase decides whether a stated rate law means CSTR or PFR, so a phase the
    # request states is preferred to the caller's guess.
    stated_phase = str(facts.get('phase') or '').strip().casefold()
    if stated_phase in ('gas', 'liquid', 'mixed'):
        if stated_phase != phase:
            report.record('reactant phase taken from the request: %r -> %r'
                          % (phase, stated_phase))
        phase = stated_phase

    request = ProcessRequest(
        source_text=source_text,
        scenario_label=scenario_label,
        components=components,
        reactions=reactions,
        feeds=[feed],
        operating_cases=cases,
        conversion_constraints=constraints,
        kinetic_data=kinetics,
        phase=phase,                                     # type: ignore[arg-type]
        # A solid is only relevant when it takes part in a reaction; a solid that is
        # merely present would not make free-energy minimisation experimental.
        has_solid_reactant=any(
            name in SOLID_COMPONENTS
            for reaction in reactions for name in reaction.stoichiometry),
    )
    if request.has_solid_reactant:
        report.assumptions.append(Assumption(
            id='a-solid-phase', field='phase', value='solid carbon present',
            source='derived', accepted=True,
            scope='进料含参与反应的固体碳，按 solid_carbon=saturation 组合流程执行'
                  '（已随工具层验收）'))

    # An equal split may be specified by the user or selected as a default.
    if _isomer_split_applies(reactions):
        stated = _equal_xylene_split_stated(source_text)
        report.assumptions.append(Assumption(
            id='a-isomer-split', field='reactions.stoichiometry',
            value='o/m/p-Xylene 各 1/3',
            source='user_text' if stated else 'agent_default', accepted=stated,
            scope=('题目明确指定三种二甲苯等摩尔分配' if stated else
                   '题目列出邻、间、对三种二甲苯但未给比例，按等分处理；'
                   '这不是工业选择性，也不是热力学预测')))

    for note in facts.get('missing_information') or []:
        if re.search(r'反应热|比热|reaction\s+heat|heat\s+capacity', str(note), re.I):
            report.record('model thermophysical-data gap ignored: HYSYS supplies property data')
            continue
        if kinetics is None and re.search(r'催化剂装填|停留时间|反应器体积|catalyst\s+mass|residence\s+time', str(note), re.I):
            report.record('model equipment-sizing gap ignored for non-kinetic calculation')
            continue
        report.notes.append('模型指出未提供：%s' % note)
    # A reviewer question and a missing-field check can describe the same gap.
    unique = {}
    for question in report.questions:
        path = {'feed_total': 'feeds[0].total_flow',
                'feed_temperature': 'feeds[0].temperature',
                'feed_pressure': 'feeds[0].pressure',
                'feed_composition': 'feeds[0].fractions'}.get(question.field, question.field)
        unique.setdefault(path, question)
    report.questions = list(unique.values())
    return request, report
