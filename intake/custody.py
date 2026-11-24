import sqlite3
from dataclasses import dataclass

from device.buffers import AttemptKey, BufferAttempt
from device.seal import SealRecord
from device.unload import UnloadOutcome
from intake.fragments import ValidatedFragment, validate_fragment
from shift.open_shift import station_write

CUSTODY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS fragment_custody (
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    event_id TEXT NOT NULL,
    attempt_key TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    raw BLOB NOT NULL,
    exit_status TEXT NOT NULL CHECK (exit_status IN ('held', 'pending', 'exited')),
    PRIMARY KEY (shift_id, event_id)
);
CREATE TABLE IF NOT EXISTS received_seals (
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    event_id TEXT NOT NULL,
    recording_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (shift_id, event_id),
    UNIQUE (recording_id, event_id),
    UNIQUE (shift_id, line_id, row_id)
);
'''


@dataclass(frozen=True)
class CaptureReceipt:
    shift_id: str
    event_id: str
    key: AttemptKey
    checksum: str
    exit_status: str


def _fragment_record(db, shift_id, event_id):
    return db.execute(
        'SELECT * FROM fragment_custody WHERE shift_id = ? AND event_id = ?', (shift_id, event_id)
    ).fetchone()


def _receipt(record) -> CaptureReceipt:
    return CaptureReceipt(
        record['shift_id'], record['event_id'], AttemptKey.decode(record['attempt_key']),
        record['checksum'], record['exit_status'],
    )


def get_capture_receipt(db: sqlite3.Connection, shift_id: str, event_id: str) -> CaptureReceipt | None:
    record = _fragment_record(db, shift_id, event_id)
    return None if record is None else _receipt(record)


def load_receipted_fragment(db: sqlite3.Connection, shift_id: str, event_id: str) -> ValidatedFragment:
    record = _fragment_record(db, shift_id, event_id)
    if record is None:
        raise LookupError(f'no fragment custody for event {event_id}')
    envelope = BufferAttempt(
        AttemptKey.decode(record['attempt_key']), record['event_id'], record['raw'], record['checksum']
    )
    fragment = validate_fragment(envelope)
    if fragment.shift_id != shift_id:
        raise ValueError('receipted fragment belongs to another shift')
    return fragment


def store_fragment(db: sqlite3.Connection, fragment: ValidatedFragment) -> CaptureReceipt:
    if not db.in_transaction:
        raise RuntimeError('fragment custody requires a station transaction')
    shift_id = fragment.shift_id
    attempt = fragment.attempt
    if load_received_seal(db, shift_id, attempt.event_id) is not None:
        raise ValueError('event identifier already names a seal')
    previous = _fragment_record(db, shift_id, attempt.event_id)
    if previous is not None:
        receipt = _receipt(previous)
        if (receipt.key, receipt.checksum, previous['raw']) != (attempt.key, attempt.checksum, attempt.raw):
            raise ValueError('fragment event replay has different content')
        return receipt
    if db.execute('SELECT 1 FROM fragment_custody WHERE attempt_key = ?', (attempt.key.encode(),)).fetchone():
        raise ValueError('physical attempt already belongs to another receipt')
    db.execute(
        'INSERT INTO fragment_custody VALUES (?, ?, ?, ?, ?, ?)',
        (shift_id, attempt.event_id, attempt.key.encode(), attempt.checksum, attempt.raw, 'held'),
    )
    return CaptureReceipt(shift_id, attempt.event_id, attempt.key, attempt.checksum, 'held')


def load_received_seal(db: sqlite3.Connection, shift_id: str, event_id: str) -> SealRecord | None:
    row = db.execute(
        'SELECT record FROM received_seals WHERE shift_id = ? AND event_id = ?', (shift_id, event_id)
    ).fetchone()
    return None if row is None else SealRecord.decode(row['record'])


def find_received_seal(db: sqlite3.Connection, shift_id: str, line_id: str, row_id: str) -> SealRecord | None:
    row = db.execute(
        'SELECT record FROM received_seals WHERE shift_id = ? AND line_id = ? AND row_id = ?',
        (shift_id, line_id, row_id),
    ).fetchone()
    return None if row is None else SealRecord.decode(row['record'])


def store_received_seal(db: sqlite3.Connection, record: SealRecord) -> SealRecord:
    if not db.in_transaction:
        raise RuntimeError('seal custody requires a station transaction')
    if get_capture_receipt(db, record.shift_id, record.event_id) is not None:
        raise ValueError('event identifier already names a fragment')
    previous = load_received_seal(db, record.shift_id, record.event_id)
    if previous is not None:
        if previous != record:
            raise ValueError('seal event replay has different content')
        return previous
    prior_for_row = find_received_seal(db, record.shift_id, record.line_id, record.row_id)
    if prior_for_row is not None:
        raise ValueError('logical row already has a different original seal')
    if db.execute(
        'SELECT 1 FROM received_seals WHERE recording_id = ? AND event_id = ?',
        (record.recording_id, record.event_id),
    ).fetchone():
        raise ValueError('seal event already belongs to another shift')
    db.execute(
        'INSERT INTO received_seals VALUES (?, ?, ?, ?, ?, ?)',
        (record.shift_id, record.event_id, record.recording_id, record.line_id, record.row_id, record.encode()),
    )
    return record


def record_exit_status(db: sqlite3.Connection, receipt: CaptureReceipt, outcome: UnloadOutcome | None) -> CaptureReceipt:
    with station_write(db):
        current = get_capture_receipt(db, receipt.shift_id, receipt.event_id)
        if current is None or (current.key, current.checksum) != (receipt.key, receipt.checksum):
            raise ValueError('exit observation does not have matching custody')
        if current.exit_status == 'exited':
            return current
        exact_exit = (
            outcome is not None and outcome.status == 'exited'
            and (outcome.key, outcome.event_id) == (current.key, current.event_id)
        )
        status = 'exited' if exact_exit else 'pending'
        db.execute(
            'UPDATE fragment_custody SET exit_status = ? WHERE shift_id = ? AND event_id = ?',
            (status, current.shift_id, current.event_id),
        )
        return CaptureReceipt(current.shift_id, current.event_id, current.key, current.checksum, status)
