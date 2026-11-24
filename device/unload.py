import sqlite3
from dataclasses import dataclass

from device.buffers import AttemptKey, release_slot

UNLOAD_SCHEMA = '''
CREATE TABLE IF NOT EXISTS unload_log (
    attempt_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL
);
'''


class DeviceUnavailable(RuntimeError):
    pass


class UnloadReplyLost(DeviceUnavailable):
    pass


@dataclass(frozen=True)
class UnloadOutcome:
    key: AttemptKey
    event_id: str | None
    status: str


def query_unload(db: sqlite3.Connection, key: AttemptKey) -> UnloadOutcome:
    row = db.execute(
        '''SELECT event_id, 'exited' AS status FROM unload_log WHERE attempt_key = ?
           UNION ALL
           SELECT event_id, 'resident' AS status FROM device_slots WHERE attempt_key = ?''',
        (key.encode(), key.encode()),
    ).fetchone()
    if row is None:
        return UnloadOutcome(key, None, 'unknown')
    return UnloadOutcome(key, row['event_id'], row['status'])


def unload_attempt(db: sqlite3.Connection, key: AttemptKey) -> tuple[UnloadOutcome, bool]:
    with db:
        db.execute('BEGIN IMMEDIATE')
        existing = query_unload(db, key)
        if existing.status == 'exited':
            return existing, False
        if existing.status != 'resident':
            raise LookupError('the requested attempt is neither resident nor previously exited')
        release_slot(db, key)
        db.execute('INSERT INTO unload_log VALUES (?, ?)', (key.encode(), existing.event_id))
        return UnloadOutcome(key, existing.event_id, 'exited'), True
