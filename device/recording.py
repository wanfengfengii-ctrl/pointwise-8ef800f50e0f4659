import hashlib
import json
import sqlite3
from dataclasses import dataclass

from device.buffers import BUFFER_SCHEMA, AttemptKey, BufferAttempt, occupy_slot, read_attempt
from device.seal import SealRecord, expose_seal
from device.unload import (
    UNLOAD_SCHEMA, DeviceUnavailable, UnloadOutcome, UnloadReplyLost, query_unload, unload_attempt,
)
from shift.lines import require_identifier

RECORDING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS recordings (
    recording_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0)
);
'''


@dataclass(frozen=True)
class _RecordingPlan:
    events: tuple[BufferAttempt | SealRecord, ...]
    by_id: dict[str, int]
    fingerprint: str


def _prepare_recording(recording_id, events) -> _RecordingPlan:
    plans = tuple(events)
    by_id = {}
    loaded = set()
    sealed = set()
    fingerprint_fields = []
    for index, event in enumerate(plans):
        if isinstance(event, BufferAttempt):
            if event.key.recording_id != recording_id or event.key in loaded:
                raise ValueError('duplicate or foreign buffer attempt')
            loaded.add(event.key)
            fingerprint_fields.append([
                'fragment', event.event_id, event.key.fields(),
                hashlib.sha256(event.raw).hexdigest(), event.checksum,
            ])
        elif isinstance(event, SealRecord):
            row = (event.shift_id, event.line_id, event.row_id)
            if event.recording_id != recording_id or row in sealed or not loaded.issuperset(event.candidates):
                raise ValueError('seal conflicts with the recording sequence')
            sealed.add(row)
            fingerprint_fields.append(['seal', event.encode()])
        else:
            raise ValueError('unknown recording event type')
        if event.event_id in by_id:
            raise ValueError('duplicate event identifier')
        by_id[event.event_id] = index
    fingerprint = hashlib.sha256(json.dumps(fingerprint_fields, ensure_ascii=True).encode('ascii')).hexdigest()
    return _RecordingPlan(plans, by_id, fingerprint)


class RecordingDevice:
    def __init__(self, path, recording_id: str, events, *, lost_unload_replies=(), query_available=True):
        self.recording_id = require_identifier(recording_id, 'recording identifier')
        self._plan = _prepare_recording(recording_id, events)
        self._lost_replies = frozenset(lost_unload_replies)
        self.query_available = query_available
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            schemas = ('BEGIN IMMEDIATE;', RECORDING_SCHEMA, BUFFER_SCHEMA, UNLOAD_SCHEMA, 'COMMIT;')
            self._db.executescript('\n'.join(schemas))
            with self._db:
                self._db.execute('BEGIN IMMEDIATE')
                self._db.execute(
                    'INSERT INTO recordings VALUES (?, ?, 0) ON CONFLICT(recording_id) DO NOTHING',
                    (self.recording_id, self._plan.fingerprint),
                )
                stored = self._db.execute(
                    'SELECT fingerprint FROM recordings WHERE recording_id = ?', (self.recording_id,)
                ).fetchone()
                if stored['fingerprint'] != self._plan.fingerprint:
                    raise ValueError('recording already exists with different events')
        except BaseException:
            self.close()
            raise

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise DeviceUnavailable('recording device is closed')
        return self._db

    @property
    def position(self) -> int:
        row = self._connection().execute(
            'SELECT position FROM recordings WHERE recording_id = ?', (self.recording_id,)
        ).fetchone()
        return row['position']

    @property
    def event_count(self) -> int:
        return len(self._plan.events)

    def next_event(self) -> BufferAttempt | SealRecord | None:
        db = self._connection()
        with db:
            db.execute('BEGIN IMMEDIATE')
            position = self.position
            if position == self.event_count:
                return None
            event = self._plan.events[position]
            if isinstance(event, BufferAttempt):
                if not occupy_slot(db, event):
                    return None
            elif expose_seal(db, event) is None:
                return None
            db.execute(
                'UPDATE recordings SET position = ? WHERE recording_id = ?', (position + 1, self.recording_id)
            )
            return event

    def read_attempt(self, key: AttemptKey) -> BufferAttempt | None:
        db = self._connection()
        if key.recording_id != self.recording_id:
            return None
        return read_attempt(db, key)

    def replay_event(self, event_id: str) -> BufferAttempt | SealRecord | None:
        position = self._plan.by_id.get(event_id)
        if position is None or position >= self.position:
            return None
        event = self._plan.events[position]
        if isinstance(event, BufferAttempt):
            return self.read_attempt(event.key)
        return event

    def query_unload(self, key: AttemptKey) -> UnloadOutcome:
        db = self._connection()
        if not self.query_available:
            raise DeviceUnavailable('unload query is unavailable')
        if key.recording_id != self.recording_id:
            return UnloadOutcome(key, None, 'unknown')
        return query_unload(db, key)

    def unload(self, key: AttemptKey) -> UnloadOutcome:
        db = self._connection()
        if key.recording_id != self.recording_id:
            raise LookupError('attempt belongs to another recording')
        outcome, newly_exited = unload_attempt(db, key)
        if newly_exited and outcome.event_id in self._lost_replies:
            raise UnloadReplyLost('device committed the exit before its reply was lost')
        return outcome

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
