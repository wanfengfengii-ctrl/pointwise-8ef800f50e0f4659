import sqlite3
from dataclasses import dataclass

from handover.issue import get_issue
from review.lookup import HandoverDelivery, read
from shift.open_shift import station_write

ACCEPT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS handover_receipts (
    number TEXT PRIMARY KEY REFERENCES handover_issues(number),
    receipt_id TEXT NOT NULL UNIQUE
);
'''


@dataclass(frozen=True)
class HandoverReceipt:
    number: str
    receipt_id: str
    delivery: HandoverDelivery


def accept(db: sqlite3.Connection, number: str) -> HandoverReceipt:
    with station_write(db):
        signed = get_issue(db, number)
        delivery = read(db, signed.number)
        previous = db.execute(
            'SELECT receipt_id FROM handover_receipts WHERE number = ?', (signed.number,)
        ).fetchone()
        if previous is not None:
            return HandoverReceipt(signed.number, previous['receipt_id'], delivery)
        receipt_id = f'acceptance-receipt/{signed.number}'
        db.execute('INSERT INTO handover_receipts VALUES (?, ?)', (signed.number, receipt_id))
        return HandoverReceipt(signed.number, receipt_id, delivery)
