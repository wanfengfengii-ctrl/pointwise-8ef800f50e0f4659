import sqlite3
from contextlib import contextmanager

from shift.config import MM_UNIT, ShiftConfig, build_shift_config, same_shift_config
from shift.lines import require_identifier

SHIFT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS shifts (
    shift_id TEXT PRIMARY KEY,
    config TEXT NOT NULL
);
'''


def connect_station(path) -> sqlite3.Connection:
    # Each state owner supplies its own schema. Initialization orders their references.
    from archive.publication import PUBLICATION_SCHEMA
    from archive.staging import STAGING_SCHEMA
    from handover.accept import ACCEPT_SCHEMA
    from handover.issue import ISSUE_SCHEMA
    from handover.revision import REISSUE_SCHEMA
    from intake.custody import CUSTODY_SCHEMA
    from review.manifest import MANIFEST_SCHEMA
    from shift.close_shift import CLOSURE_SCHEMA

    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys = ON')
        schemas = (
            SHIFT_SCHEMA, CUSTODY_SCHEMA, STAGING_SCHEMA, PUBLICATION_SCHEMA,
            MANIFEST_SCHEMA, ISSUE_SCHEMA, ACCEPT_SCHEMA, REISSUE_SCHEMA, CLOSURE_SCHEMA,
        )
        db.executescript('\n'.join(('BEGIN IMMEDIATE;', *schemas, 'COMMIT;')))
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def station_write(db: sqlite3.Connection):
    if db.in_transaction:
        raise RuntimeError('station transaction already open')
    with db:
        db.execute('BEGIN IMMEDIATE')
        yield


def get_shift(db: sqlite3.Connection, shift_id: str) -> ShiftConfig:
    require_identifier(shift_id, 'shift identifier')
    row = db.execute('SELECT config FROM shifts WHERE shift_id = ?', (shift_id,)).fetchone()
    if row is None:
        raise LookupError(f'unknown shift: {shift_id}')
    return ShiftConfig.decode(row['config'])


def open_shift(db: sqlite3.Connection, shift_id: str, line_ids, unit: str = MM_UNIT) -> ShiftConfig:
    config = build_shift_config(shift_id, line_ids, unit)
    with station_write(db):
        row = db.execute('SELECT config FROM shifts WHERE shift_id = ?', (shift_id,)).fetchone()
        if row is not None:
            existing = ShiftConfig.decode(row['config'])
            if not same_shift_config(existing, config):
                raise ValueError('shift already has a different configuration')
            return existing
        db.execute('INSERT INTO shifts VALUES (?, ?)', (shift_id, config.encode()))
    return config
