import json
import sqlite3
from dataclasses import asdict, dataclass

from archive.publication import Publication, get_publication
from review.totals import LineTotals, summarize_manifest, summarize_readings

MANIFEST_SCHEMA = '''
CREATE TABLE IF NOT EXISTS manifests (
    number TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    totals TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifest_entries (
    number TEXT NOT NULL REFERENCES manifests(number),
    ordinal INTEGER NOT NULL,
    line_id TEXT NOT NULL,
    publication_number TEXT NOT NULL REFERENCES publication_receipts(number),
    totals TEXT NOT NULL,
    PRIMARY KEY (number, ordinal),
    UNIQUE (number, line_id)
);
'''


@dataclass(frozen=True)
class ManifestEntry:
    line_id: str
    publication_number: str
    totals: LineTotals


@dataclass(frozen=True)
class ShiftManifest:
    number: str
    shift_id: str
    entries: tuple[ManifestEntry, ...]
    totals: LineTotals


def _manifest_entry(db: sqlite3.Connection, shift_id: str, publication: Publication) -> ManifestEntry:
    verified = get_publication(db, publication.receipt.number)
    if verified != publication or verified.row.shift_id != shift_id:
        raise ValueError('manifest requires the exact committed publication in this shift')
    return ManifestEntry(verified.row.line_id, verified.receipt.number, summarize_readings(verified.row.readings))


def freeze_manifest(
    db: sqlite3.Connection, number: str, shift_id: str, publications: tuple[Publication, ...]
) -> ShiftManifest:
    if not db.in_transaction:
        raise RuntimeError('manifest freezing requires the handover transaction')
    entries = tuple(_manifest_entry(db, shift_id, publication) for publication in publications)
    if not entries or len({entry.line_id for entry in entries}) != len(entries):
        raise ValueError('manifest requires a nonempty set of distinct lines')
    totals = summarize_manifest(entry.totals for entry in entries)
    db.execute('INSERT INTO manifests VALUES (?, ?, ?)', (number, shift_id, json.dumps(asdict(totals))))
    db.executemany(
        'INSERT INTO manifest_entries VALUES (?, ?, ?, ?, ?)',
        ((number, index, entry.line_id, entry.publication_number, json.dumps(asdict(entry.totals)))
         for index, entry in enumerate(entries)),
    )
    return ShiftManifest(number, shift_id, entries, totals)


def get_manifest(db: sqlite3.Connection, number: str) -> ShiftManifest:
    row = db.execute('SELECT shift_id, totals FROM manifests WHERE number = ?', (number,)).fetchone()
    if row is None:
        raise LookupError(f'unknown manifest: {number}')
    items = db.execute(
        '''SELECT line_id, publication_number, totals FROM manifest_entries
           WHERE number = ? ORDER BY ordinal''', (number,)
    )
    entries = tuple(
        ManifestEntry(item['line_id'], item['publication_number'], LineTotals(**json.loads(item['totals'])))
        for item in items
    )
    return ShiftManifest(number, row['shift_id'], entries, LineTotals(**json.loads(row['totals'])))
