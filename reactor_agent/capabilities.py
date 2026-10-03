"""Machine-readable capability lookup for reactor/model combinations.

The tool layer already publishes `hysys_tools.capabilities.capability_metadata()`.
This module turns that list into a lookup that answers one question precisely:

    given (reactor kind, thermal mode, phase, reaction count, solid phase),
    is this combination verified, experimental or unsupported, and on what
    evidence?

Why a lookup rather than prose: the plan's complaint about the old capability
table was that "Gibbs is verified" reads as though every Gibbs combination were
verified, including adiabatic operation and solid carbon, which it is not. The
answer the agent needs is per combination.

The `verified` rows are bound to one acceptance record, not to a date. The record
names the accepted tool revision and the SHA256 of every runtime file, so the
claim "this combination was verified" is only made while the tool layer still
matches the bytes that were verified. Change one byte and every `verified`
becomes `experimental` with the reason attached. The tool layer's own
`capabilities.py` still says `pending` for the current revision; that is the text
it shipped with and this module does not touch it.

Nothing here invents status. Every entry either cites a real HYSYS result on this
workstation or says plainly that there is none.
"""
from __future__ import annotations

import functools
import hashlib
from pathlib import Path
from typing import Any

import hysys_tools
from hysys_tools import capabilities as tool_capabilities

# --------------------------------------------------------------------- record
# The acceptance this module trusts. Copied from MANIFEST.json, which is stored
# as docs/tool-acceptance-20261003-105341.json.
ACCEPTED_TOOL_REVISION = '2026-10-03-equilibrium-integration-1'
ACCEPTED_RUN = '20261003-105341-4467d9c9'
ACCEPTED_STATUS = 'ALL_SCENARIOS_PASS'
ACCEPTANCE_RECORD = 'docs/tool-acceptance-20261003-105341.json'
ACCEPTANCE_EVIDENCE = 'tool-layer-runs/acceptance-20261003-105341-4467d9c9'
ACCEPTED_SHA256 = {
    '__init__.py': 'b2366bc487d0d94afee96598fa2b621cf884a8101bd0619d346dca477ef389a1',
    '__main__.py': '93d83f4af888721746b822c3e064d71ec8531308d796eac92ab8ea8d27e24679',
    'capabilities.py': '259c9e94d5123a39ac47f35afb693efc10cc7167716bf53304fc4f8dfe8aa35e',
    'core.py': 'e23f151968c18ca2b851cc9f4b8c9bde76ab1cd6c4c3e7d6f07a1ba1f6946e88',
    'equilibrium.py': 'cbe3089a0849a939bf547557a1aa85ed990f10783a368d5139a97f9a2d5c607c',
    'examples.py': 'b2077d7b1acbf38089fd6b95a87c565c971c83eb3945668511cc34d003f50605',
    'main.py': 'd0ab80500be3b4040f473a0197aa188cc69bbd798d12500423e5791e2204ea2f',
    'native_flow.py': '5e759b63f50348c829d66036aab04308f72a8e4e2688dfd287256553731b8c62',
    'precheck.py': '1da6dc12f8406c316e0ea2286a375d5b3dd86f42a6fc7e084a5a1616b586fe69',
    'reactor.py': '604a3a9ead0c36992cb45df74aa5926132e8982d176140920f9d9be1258dcb9a',
    'saturation.py': '332ce5ad9a9a8d34dded8b4298d44b4e53fc3b12d82134ea0aa6dfe3ac8fc675',
    'thermo_reference.py': '7bd6c4e98126276ef7218c348094a9c57cf282940ed5dde7201dfb5fc842aa5b',
    'validate.py': '8ca7608e6700c5d1bac8b7847a8e16dce2411b945602d59c6f9f60b49a67ae49',
}

# Historical evidence kept alongside the acceptance record. These runs were
# accepted earlier and their numbers still back the toluene and gas-phase Gibbs
# rows, so they stay citable.
EVIDENCE_ROOT = 'tool-layer-runs/20261002-111439'
REMOTE_ACCEPTANCE = 'tool-layer-runs/acceptance-20261002-144131-d442afae'

_ACCEPTANCE_EVIDENCE = '%s（验收 %s，%s）' % (
    ACCEPTANCE_RECORD, ACCEPTED_RUN, ACCEPTED_STATUS)


def _file_sha256(path: Path) -> str:
    """SHA256 of one file. Separate so tests can patch it."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


@functools.lru_cache(maxsize=1)
def acceptance_state() -> dict[str, Any]:
    """Does the installed tool layer still match the accepted bytes?

    Cached because this hashes 13 files; tests call
    `acceptance_state.cache_clear()` after patching `_file_sha256`.
    """
    installed_revision = str(getattr(tool_capabilities, 'TOOL_REVISION', '') or '')
    tool_dir = Path(hysys_tools.__file__).parent

    mismatched: list[str] = []
    if installed_revision != ACCEPTED_TOOL_REVISION:
        mismatched.append('TOOL_REVISION')

    for name, expected in ACCEPTED_SHA256.items():
        path = tool_dir / name
        try:
            actual = _file_sha256(path)
        except OSError:
            mismatched.append(name)
            continue
        if actual != expected:
            mismatched.append(name)

    holds = not mismatched
    if holds:
        reason = ('工具层与验收 %s（%s，%s）逐字节一致，验收结论可用。'
                  % (ACCEPTED_TOOL_REVISION, ACCEPTED_RUN, ACCEPTED_STATUS))
    else:
        reason = ('工具层与验收 %s 不一致（%s），验收结论不再成立，'
                  '所有经验收的组合降为 experimental。'
                  % (ACCEPTED_TOOL_REVISION, '、'.join(mismatched)))

    return {'holds': holds, 'tool_revision': installed_revision,
            'mismatched': mismatched, 'reason': reason}


def _downgrade(status: dict[str, Any], acceptance: dict[str, Any]) -> dict[str, Any]:
    """Turn a `verified` row into `experimental` while the bytes do not match."""
    return {'status': 'experimental',
            'reason': '%s %s' % (acceptance['reason'], status['reason']),
            'evidence': [],
            'rule': status['rule']}


def _acceptance_evidence() -> list[str]:
    return [_ACCEPTANCE_EVIDENCE, ACCEPTANCE_EVIDENCE]


def capability_report() -> dict[str, Any]:
    """The tool layer's capability metadata, plus what the agent adds to it."""
    acceptance = acceptance_state()
    report = dict(tool_capabilities.capability_metadata())
    report['agent_notes'] = {
        'accepted_tool_revision': ACCEPTED_TOOL_REVISION,
        'accepted_run': ACCEPTED_RUN,
        'accepted_status': ACCEPTED_STATUS,
        'acceptance_record': ACCEPTANCE_RECORD,
        'acceptance_evidence': ACCEPTANCE_EVIDENCE,
        'acceptance_state': acceptance,
        'tool_revision_matches_acceptance': acceptance['holds'],
        'evidence_root': EVIDENCE_ROOT,
        'remote_acceptance': REMOTE_ACCEPTANCE,
        'remote_acceptance_status': 'BASELINE_PASS_GASIFICATION_STILL_BLOCKED',
        'remote_acceptance_scope': (
            'toluene (conversion, adiabatic), SMR 710 C and 600 C (gibbs, '
            'isothermal). All three matched the historical baseline bit for bit. '
            'Superseded for the current decisions by acceptance %s, which also '
            'covers equilibrium reforming and the saturated-carbon gasification '
            'route.' % ACCEPTED_RUN),
        'not_proven_by_acceptance': [
            'isothermal conversion',
            'more than one conversion reaction',
            'adiabatic gibbs and adiabatic equilibrium',
            'equilibrium with a liquid phase or with a solid component taking part',
            'gasification feeds other than carbon plus water',
            'CSTR or PFR of any kind',
        ],
    }
    return report


def _unsupported(reason: str, rule: str) -> dict[str, Any]:
    return {'status': 'unsupported', 'reason': reason, 'evidence': [],
            'rule': rule}


def combination_status(reactor_kind: str, thermal_mode: str | None = None,
                       phase: str | None = None, reaction_count: int = 1,
                       solid_phase: bool = False) -> dict[str, Any]:
    """Status of one reactor/model combination.

    `solid_phase` means a solid component actually takes part (carbon in the
    gasifier), not merely that one appears in the component list.
    """
    kind = str(reactor_kind).strip().casefold()
    thermal = None if thermal_mode is None else str(thermal_mode).strip().casefold()
    combination_phase = None if phase is None else str(phase).strip().casefold()
    verified = acceptance_state()['holds']

    # ------------------------------------------------------- not implemented
    if kind in ('cstr', 'pfr'):
        return _unsupported(
            '工具层没有 %s 的实现。题目的选型规则会点名动力学反应器，'
            '但没有任何场景给出速率方程，所以这是已声明的能力边界，'
            '不是建模失败。' % kind.upper(), 'no_implementation')

    # ------------------------------------------------------------- conversion
    if kind == 'conversion':
        if reaction_count > 1:
            return _unsupported(
                '多个反应共用同一个基准组分时，独立转化率校核无法把各自的转化率分开，'
                '因此工具层拒绝一个以上的转化率反应，而不是让总转化率冒名顶替'
                '每个反应的转化率。', 'multiple_conversion_reactions')
        if thermal == 'adiabatic':
            row = {
                'status': 'verified',
                'reason': '单个固定转化率反应，绝热，热负荷固定为零。',
                'evidence': ([_ACCEPTANCE_EVIDENCE, ACCEPTANCE_EVIDENCE]
                             + [EVIDENCE_ROOT + '/toluene/result.json',
                                REMOTE_ACCEPTANCE + '/toluene/result.json']),
                'rule': 'conversion_adiabatic_single'}
            return row if verified else _downgrade(row, acceptance_state())
        if thermal == 'isothermal':
            return {'status': 'experimental',
                    'reason': '等温 Conversion 没有经真机验收的运行；代码路径存在。',
                    'evidence': [], 'rule': 'conversion_isothermal_single'}
        return {'status': 'experimental',
                'reason': '未给出热边界；已验收的 Conversion 场景是绝热的。',
                'evidence': [], 'rule': 'conversion_unspecified_thermal'}

    # ------------------------------------------------------------- equilibrium
    if kind == 'equilibrium':
        if solid_phase:
            return _unsupported(
                'Equilibrium 反应器不接受参与反应的固体组分。气化的固体碳走的是'
                'Gibbs 的饱和碳组合流程，不是这一条。', 'equilibrium_solid')
        if combination_phase == 'liquid':
            return _unsupported(
                'Equilibrium 反应器只接受气相反应。液相反应需要活度系数模型，'
                '工具层没有这条路径。', 'equilibrium_liquid')
        if thermal != 'isothermal':
            return _unsupported(
                'Equilibrium 反应器必须给出等温出口温度才能设置 Ln(K) 来源；'
                '绝热或未给出热边界时工具层拒绝该路径。',
                'equilibrium_needs_isothermal')
        row = {
            'status': 'verified',
            'reason': ('等温 Equilibrium、气相反应。已验收的算例是甲烷蒸汽重整的'
                       '两个工况（710 C 与 600 C），平衡常数由 HYSYS 组分 Gibbs '
                       '数据在出口温度附近拟合，工具层会用出口 Q/K 校核。'),
            'evidence': ([_ACCEPTANCE_EVIDENCE, ACCEPTANCE_EVIDENCE]
                         + [EVIDENCE_ROOT + '/smr-710C/result.json',
                            EVIDENCE_ROOT + '/smr-600C/result.json',
                            REMOTE_ACCEPTANCE + '/smr-710C/result.json',
                            REMOTE_ACCEPTANCE + '/smr-600C/result.json']),
            'rule': 'equilibrium_isothermal_vapour'}
        return row if verified else _downgrade(row, acceptance_state())

    # ------------------------------------------------------------------ gibbs
    if kind == 'gibbs':
        if thermal == 'adiabatic':
            return _unsupported(
                'Gibbs 反应器没有绝热实现：执行器不会为它设置热边界，'
                '工况就没有声明的边界，也从未验收过。',
                'gibbs_adiabatic_unimplemented')
        if thermal != 'isothermal':
            return {'status': 'experimental',
                    'reason': '未给出热边界；只有等温 Gibbs 经过验收。',
                    'evidence': [], 'rule': 'gibbs_unspecified_thermal'}
        if solid_phase:
            row = {
                'status': 'verified',
                'reason': ('仅验收了"碳 + 水"进料的饱和碳路线；走的是 '
                           'solid_carbon=saturation 组合流程（转化率反应器加'
                           '仅含气相的 Gibbs 反应器，外层求解使气相碳活度为 1），'
                           '不是直接用库 Carbon 的单台 Gibbs 反应器；'
                           '未反应的碳会出现在名为 LIQUID 的物流里，实为固相。'),
                'evidence': _acceptance_evidence(),
                'rule': 'gibbs_isothermal_solid_saturation'}
            return row if verified else _downgrade(row, acceptance_state())
        row = {
            'status': 'verified',
            'reason': ('等温 Gibbs，气相候选产物集合。已验收的算例是 '
                       'CH4/H2O/CO/H2/CO2 的甲烷蒸汽重整，710 C 与 600 C。'),
            'evidence': ([_ACCEPTANCE_EVIDENCE, ACCEPTANCE_EVIDENCE]
                         + [EVIDENCE_ROOT + '/smr-710C/result.json',
                            EVIDENCE_ROOT + '/smr-600C/result.json',
                            REMOTE_ACCEPTANCE + '/smr-710C/result.json',
                            REMOTE_ACCEPTANCE + '/smr-600C/result.json']),
            'rule': 'gibbs_isothermal_gas'}
        return row if verified else _downgrade(row, acceptance_state())

    return _unsupported('未知或未处理的反应器类型：%r' % reactor_kind,
                        'unknown_kind')


def is_executable(reactor_kind: str, thermal_mode: str | None = None,
                  phase: str | None = None, reaction_count: int = 1,
                  solid_phase: bool = False) -> bool:
    """True when the tool layer can actually build and run the combination."""
    return combination_status(reactor_kind, thermal_mode, phase=phase,
                              reaction_count=reaction_count,
                              solid_phase=solid_phase)['status'] != 'unsupported'
