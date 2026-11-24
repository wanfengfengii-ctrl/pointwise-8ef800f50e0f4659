import sqlite3
from dataclasses import dataclass

from device.buffers import AttemptKey
from intake.custody import load_receipted_fragment
from shift.lines import require_line
from shift.open_shift import get_shift

STAGING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS staged_fragments (
    id INTEGER PRIMARY KEY,
    shift_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    attempt_key TEXT NOT NULL UNIQUE,
    line_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('whole', 'head', 'candidate')),
    UNIQUE (shift_id, event_id),
    FOREIGN KEY (shift_id, event_id) REFERENCES fragment_custody(shift_id, event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_staged_anchor
    ON staged_fragments(shift_id, line_id) WHERE kind IN ('whole', 'head');
CREATE INDEX IF NOT EXISTS staged_line_lookup ON staged_fragments(shift_id, line_id);
'''


@dataclass(frozen=True)
class StagedFragment:
    shift_id: str
    event_id: str
    key: AttemptKey
    line_id: str
    row_id: str
    kind: str


def _staged_row(row) -> StagedFragment:
    return StagedFragment(
        row['shift_id'], row['event_id'], AttemptKey.decode(row['attempt_key']),
        row['line_id'], row['row_id'], row['kind'],
    )


def stage_fragment(db: sqlite3.Connection, shift_id: str, event_id: str) -> StagedFragment:
    if not db.in_transaction:
        raise RuntimeError('staging requires the custody transaction')
    fragment = load_receipted_fragment(db, shift_id, event_id)
    if fragment.kind == 'retest':
        raise ValueError('retest pieces back errata and must not be staged for publication')
    require_line(get_shift(db, shift_id).roster, fragment.line_id)
    expected = StagedFragment(
        shift_id, event_id, fragment.attempt.key, fragment.line_id, fragment.row_id, fragment.kind
    )
    existing = db.execute(
        'SELECT * FROM staged_fragments WHERE shift_id = ? AND event_id = ?', (shift_id, event_id)
    ).fetchone()
    if existing is not None:
        if _staged_row(existing) != expected:
            raise ValueError('staging disagrees with the original receipt')
        return expected
    siblings = list_staged_fragments(db, shift_id, fragment.line_id)
    if fragment.kind != 'candidate' and any(item.kind != 'candidate' for item in siblings):
        raise ValueError('line already has a different ordinary piece or old-roll head')
    if fragment.kind == 'whole' and siblings or any(item.kind == 'whole' for item in siblings):
        raise ValueError('ordinary and rollover pieces cannot share one line')
    if fragment.kind == 'candidate':
        same_row = [item for item in siblings if item.kind == 'candidate' and item.row_id == fragment.row_id]
        if len(same_row) >= 2:
            raise ValueError('logical row already has two candidate attempts')
    db.execute(
        '''INSERT INTO staged_fragments (shift_id, event_id, attempt_key, line_id, row_id, kind)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (shift_id, event_id, expected.key.encode(), expected.line_id, expected.row_id, expected.kind),
    )
    return expected


def list_staged_fragments(db: sqlite3.Connection, shift_id: str, line_id: str) -> tuple[StagedFragment, ...]:
    rows = db.execute(
        'SELECT * FROM staged_fragments WHERE shift_id = ? AND line_id = ? ORDER BY id', (shift_id, line_id)
    )
    return tuple(_staged_row(row) for row in rows)
