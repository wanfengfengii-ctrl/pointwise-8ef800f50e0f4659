import json
import sqlite3
from dataclasses import asdict, dataclass
from urllib.parse import quote

from archive.assembly import AssembledRow, InvalidSpan, assemble_row
from archive.eligibility import check_eligibility
from device.buffers import AttemptKey
from device.seal import SealRecord
from intake.fragments import FragmentOrigin
from shift.lines import require_identifier, require_line
from shift.open_shift import get_shift, station_write

PUBLICATION_SCHEMA = '''
CREATE TABLE IF NOT EXISTS publications (
    number TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    line_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (shift_id, line_id)
);
CREATE TABLE IF NOT EXISTS publication_receipts (
    number TEXT PRIMARY KEY REFERENCES publications(number),
    receipt_id TEXT NOT NULL UNIQUE
);
'''


class PublicationBlocked(LookupError):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class PublicationReceipt:
    number: str
    receipt_id: str


@dataclass(frozen=True)
class Publication:
    row: AssembledRow
    receipt: PublicationReceipt


def publication_number(shift_id: str, line_id: str) -> str:
    shift = quote(require_identifier(shift_id, 'shift identifier'), safe='')
    line = quote(require_identifier(line_id, 'line identifier'), safe='')
    return f'row/{shift}/{line}'


def _encode_body(row: AssembledRow) -> str:
    return json.dumps({
        'readings': row.readings,
        'origins': [asdict(origin) for origin in row.origins],
        'seal': row.seal.encode() if row.seal is not None else None,
    }, ensure_ascii=True)


def _decode_publication(record) -> Publication:
    body = json.loads(record['body'])
    origins = tuple(
        FragmentOrigin(AttemptKey(**item['key']), item['event_id'], item['start'], item['stop'], item['checksum'])
        for item in body['origins']
    )
    seal = SealRecord.decode(body['seal']) if body['seal'] is not None else None
    row = AssembledRow(record['shift_id'], record['line_id'], record['row_id'], tuple(body['readings']), origins, seal)
    return Publication(row, PublicationReceipt(record['number'], record['receipt_id']))


def _find_publication(db: sqlite3.Connection, number: str) -> Publication | None:
    row = db.execute(
        '''SELECT p.*, r.receipt_id FROM publications p
           JOIN publication_receipts r ON r.number = p.number WHERE p.number = ?''', (number,)
    ).fetchone()
    return None if row is None else _decode_publication(row)


def get_publication(db: sqlite3.Connection, number: str) -> Publication:
    result = _find_publication(db, number)
    if result is None:
        raise LookupError(f'unknown or unsigned publication: {number}')
    return result


def publish(db: sqlite3.Connection, shift_id: str, line_id: str) -> Publication:
    number = publication_number(shift_id, line_id)
    with station_write(db):
        require_line(get_shift(db, shift_id).roster, line_id)
        existing = _find_publication(db, number)
        if existing is not None:
            return existing
        eligibility = check_eligibility(db, shift_id, line_id)
        if eligibility.status != 'ready':
            raise PublicationBlocked(eligibility.status)
        try:
            row = assemble_row(db, eligibility.fragments, eligibility.seal)
        except InvalidSpan as exc:
            raise PublicationBlocked('invalid_span') from exc
        receipt = PublicationReceipt(number, f'publication-receipt/{number}')
        db.execute(
            'INSERT INTO publications VALUES (?, ?, ?, ?, ?)',
            (number, row.shift_id, row.line_id, row.row_id, _encode_body(row)),
        )
        db.execute('INSERT INTO publication_receipts VALUES (?, ?)', (number, receipt.receipt_id))
        return Publication(row, receipt)
