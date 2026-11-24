import json
import sqlite3
from dataclasses import dataclass

from shift.lines import require_identifier

BUFFER_SCHEMA = '''
CREATE TABLE IF NOT EXISTS device_slots (
    recording_id TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot IN (0, 1)),
    attempt_key TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    raw BLOB NOT NULL,
    checksum TEXT NOT NULL,
    PRIMARY KEY (recording_id, slot)
);
'''


@dataclass(frozen=True)
class AttemptKey:
    recording_id: str
    roll: str
    slot: int
    load: int

    def __post_init__(self):
        require_identifier(self.recording_id, 'recording identifier')
        require_identifier(self.roll, 'roll identifier')
        if type(self.slot) is not int or self.slot not in (0, 1):
            raise ValueError('invalid device slot')
        if type(self.load) is not int or self.load < 1:
            raise ValueError('invalid buffer load number')

    def fields(self) -> list:
        return [self.recording_id, self.roll, self.slot, self.load]

    def encode(self) -> str:
        return json.dumps(self.fields(), ensure_ascii=True, separators=(',', ':'))

    @classmethod
    def decode(cls, text: str) -> 'AttemptKey':
        return cls(*json.loads(text))


@dataclass(frozen=True)
class BufferAttempt:
    key: AttemptKey
    event_id: str
    raw: bytes
    checksum: str

    def __post_init__(self):
        if not isinstance(self.key, AttemptKey) or not isinstance(self.raw, bytes):
            raise ValueError('invalid buffer envelope')
        require_identifier(self.event_id, 'event identifier')
        if not isinstance(self.checksum, str):
            raise ValueError('invalid checksum envelope')


def occupy_slot(db: sqlite3.Connection, attempt: BufferAttempt) -> bool:
    if not db.in_transaction:
        raise RuntimeError('loading a buffer requires a device transaction')
    key = attempt.key
    result = db.execute(
        '''INSERT INTO device_slots VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(recording_id, slot) DO NOTHING''',
        (key.recording_id, key.slot, key.encode(), attempt.event_id, attempt.raw, attempt.checksum),
    )
    return result.rowcount == 1


def read_attempt(db: sqlite3.Connection, key: AttemptKey) -> BufferAttempt | None:
    row = db.execute(
        'SELECT event_id, raw, checksum FROM device_slots WHERE attempt_key = ?', (key.encode(),)
    ).fetchone()
    if row is None:
        return None
    return BufferAttempt(key, row['event_id'], row['raw'], row['checksum'])


def release_slot(db: sqlite3.Connection, key: AttemptKey) -> None:
    if not db.in_transaction:
        raise RuntimeError('releasing a buffer requires a device transaction')
    result = db.execute('DELETE FROM device_slots WHERE attempt_key = ?', (key.encode(),))
    if result.rowcount != 1:
        raise LookupError('the requested attempt is absent from its buffer')
