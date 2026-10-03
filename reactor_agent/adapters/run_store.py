"""Execution ledger: what has already been run, and with which specification.

Needed because a HYSYS run is an expensive side effect that cannot be undone. If the
process restarts, or a user clicks submit twice, the agent must be able to tell
"this case has already produced a result with exactly this spec" from "this case
still needs running" - without re-running anything.

The ledger is append-only JSONL. Appends survive a crash; a rewritten file does not,
and losing the record of a side effect is worse than an extra line.

Identity is `case_id + spec_hash`. The same case id with a different spec is a
different job: a resumed run must not reuse a result that came from other inputs.

Nothing sensitive is stored. There is no place here for a credential, a base URL or
a prompt.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

LEDGER_NAME = 'ledger.jsonl'

# A result is only reusable when it actually succeeded. A failed attempt is retried;
# a refused-before-execution case is not an outcome at all.
REUSABLE_STATUSES = ('PASS',)


@dataclass
class LedgerEntry:
    """One attempt, as recorded."""
    case_id: str
    spec_hash: str
    attempt: int
    status: str
    run_dir: str = ''
    seconds: float = 0.0
    exit_code: int | None = None
    tool_status: str | None = None
    error_type: str | None = None
    error: str | None = None
    case_file: str | None = None
    # Kept short on purpose: the ledger records outcomes, not payloads.
    notes: list[str] = field(default_factory=list)
    finished_at: str = ''

    def __post_init__(self) -> None:
        if not self.finished_at:
            self.finished_at = datetime.now().isoformat(timespec='seconds')

    @property
    def reusable(self) -> bool:
        return self.status in REUSABLE_STATUSES

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def hash_spec(spec: dict[str, Any] | None) -> str:
    """Stable hash of a spec, independent of key order."""
    if spec is None:
        return ''
    payload = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class RunStore:
    """The ledger for one run directory."""

    def __init__(self, root: Path, run_id: str = '') -> None:
        self.root = Path(root)
        self.run_id = run_id or self.root.name
        self.path = self.root / LEDGER_NAME
        self._cache: list[LedgerEntry] | None = None

    # ---------------------------------------------------------------- writing
    def record(self, entry: LedgerEntry) -> LedgerEntry:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(entry.to_json() + '\n')
        if self._cache is not None:
            self._cache.append(entry)
        return entry

    def record_result(self, case_id: str, spec: dict[str, Any] | None,
                      attempt: int, status: str, **fields: Any) -> LedgerEntry:
        """Convenience wrapper that computes the spec hash for the caller."""
        return self.record(LedgerEntry(case_id=case_id, spec_hash=hash_spec(spec),
                                       attempt=attempt, status=status, **fields))

    # ---------------------------------------------------------------- reading
    def entries(self) -> list[LedgerEntry]:
        if self._cache is not None:
            return list(self._cache)
        found: list[LedgerEntry] = []
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    found.append(LedgerEntry(**json.loads(line)))
                except Exception:                       # noqa: BLE001
                    # A truncated final line is possible after a hard kill. Skip it;
                    # the alternative is refusing to resume at all.
                    continue
        self._cache = found
        return list(found)

    def for_case(self, case_id: str) -> list[LedgerEntry]:
        return [e for e in self.entries() if e.case_id == case_id]

    def attempts(self, case_id: str) -> int:
        return len(self.for_case(case_id))

    def completed(self, case_id: str, spec: dict[str, Any] | None) -> LedgerEntry | None:
        """A successful previous attempt with exactly this spec, if there is one."""
        wanted = hash_spec(spec)
        for entry in reversed(self.for_case(case_id)):
            if entry.spec_hash == wanted and entry.reusable:
                return entry
        return None

    def needs_running(self, case_id: str, spec: dict[str, Any] | None) -> bool:
        """True unless this exact job has already succeeded.

        A case whose spec changed needs running again - that is the point of hashing
        the spec rather than remembering only the case id.
        """
        return self.completed(case_id, spec) is None

    # ------------------------------------------------------------- summaries
    def summary(self) -> dict[str, Any]:
        entries = self.entries()
        by_status: dict[str, int] = {}
        for entry in entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
        cases = sorted({e.case_id for e in entries})
        return {
            'run_id': self.run_id,
            'ledger': str(self.path),
            'attempts': len(entries),
            'cases': cases,
            'by_status': by_status,
            'succeeded': sorted({e.case_id for e in entries if e.reusable}),
            'pending': [c for c in cases if self.completed(c, None) is None
                        and not any(e.case_id == c and e.reusable for e in entries)],
        }

    def manifest(self) -> dict[str, Any]:
        """Everything needed to reproduce this run, minus anything sensitive."""
        return {'run_id': self.run_id, 'root': str(self.root),
                'ledger': str(self.path), 'entries': [asdict(e) for e in self.entries()]}


def load_entries(paths: Iterable[Path]) -> list[LedgerEntry]:
    """Read several ledgers in order, for a cross-run view."""
    out: list[LedgerEntry] = []
    for path in paths:
        if Path(path).is_file():
            out.extend(RunStore(Path(path).parent).entries()
                       if Path(path).name == LEDGER_NAME else [])
    return out
