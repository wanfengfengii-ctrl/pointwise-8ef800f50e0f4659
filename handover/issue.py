import sqlite3
from dataclasses import dataclass
from urllib.parse import quote

from archive.publication import get_publication, publication_number
from review.manifest import ShiftManifest, freeze_manifest, get_manifest
from shift.close_shift import ShiftClosure, close_shift
from shift.lines import parse_line_roster, require_identifier, require_line
from shift.open_shift import get_shift, station_write

ISSUE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS handover_issues (
    number TEXT PRIMARY KEY REFERENCES manifests(number),
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id)
);
'''


@dataclass(frozen=True)
class HandoverIssue:
    number: str
    manifest: ShiftManifest


def handover_number(request_id: str) -> str:
    return 'handover/' + quote(require_identifier(request_id, 'handover request identifier'), safe='')


def get_issue(db: sqlite3.Connection, number: str) -> HandoverIssue:
    row = db.execute('SELECT shift_id FROM handover_issues WHERE number = ?', (number,)).fetchone()
    if row is None:
        raise LookupError(f'unknown or unsigned handover: {number}')
    manifest = get_manifest(db, number)
    if manifest.shift_id != row['shift_id']:
        raise ValueError('handover issue and manifest disagree on shift')
    return HandoverIssue(number, manifest)


def issue(db: sqlite3.Connection, shift_id: str, request_id: str, line_ids) -> HandoverIssue:
    number = handover_number(request_id)
    requested = parse_line_roster(line_ids).line_ids
    with station_write(db):
        roster = get_shift(db, shift_id).roster
        try:
            existing = get_issue(db, number)
        except LookupError:
            existing = None
        if existing is not None:
            existing_lines = tuple(entry.line_id for entry in existing.manifest.entries)
            if (existing.manifest.shift_id, existing_lines) != (shift_id, requested):
                raise ValueError('handover number already has a different request')
            return existing
        publications = tuple(
            get_publication(db, publication_number(shift_id, require_line(roster, line_id)))
            for line_id in requested
        )
        manifest = freeze_manifest(db, number, shift_id, publications)
        db.execute('INSERT INTO handover_issues VALUES (?, ?)', (number, shift_id))
        return HandoverIssue(number, manifest)


def finish_shift_handover(db: sqlite3.Connection, shift_id: str, number: str) -> ShiftClosure:
    return close_shift(db, shift_id, get_issue(db, number).number)
