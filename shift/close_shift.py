import sqlite3
from dataclasses import dataclass

from shift.config import ShiftConfig
from shift.open_shift import get_shift, station_write

CLOSURE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS shift_closures (
    shift_id TEXT PRIMARY KEY REFERENCES shifts(shift_id),
    issue_number TEXT NOT NULL REFERENCES handover_issues(number),
    config TEXT NOT NULL
);
'''


@dataclass(frozen=True)
class ShiftClosure:
    shift_id: str
    issue_number: str
    config: ShiftConfig


def get_closure(db: sqlite3.Connection, shift_id: str) -> ShiftClosure | None:
    row = db.execute(
        'SELECT issue_number, config FROM shift_closures WHERE shift_id = ?', (shift_id,)
    ).fetchone()
    if row is None:
        return None
    return ShiftClosure(shift_id, row['issue_number'], ShiftConfig.decode(row['config']))


def close_shift(db: sqlite3.Connection, shift_id: str, issue_number: str) -> ShiftClosure:
    with station_write(db):
        config = get_shift(db, shift_id)
        issue = db.execute(
            'SELECT shift_id FROM handover_issues WHERE number = ?', (issue_number,)
        ).fetchone()
        if issue is None or issue['shift_id'] != shift_id:
            raise LookupError('closure requires an issued handover for this shift')
        previous = get_closure(db, shift_id)
        if previous is not None:
            if previous.issue_number != issue_number:
                raise ValueError('shift closure already has a different handover number')
            return previous
        db.execute('INSERT INTO shift_closures VALUES (?, ?, ?)', (shift_id, issue_number, config.encode()))
        return ShiftClosure(shift_id, issue_number, config)
