import sqlite3
from dataclasses import dataclass

from archive.publication import Publication, PublicationReceipt, get_publication
from device.seal import SealRecord
from review.manifest import ShiftManifest, get_manifest
from review.provenance import ProvenanceSlice, project_provenance
from review.totals import LineTotals, summarize_readings
from shift.lines import require_identifier


@dataclass(frozen=True)
class LineDelivery:
    shift_id: str
    line_id: str
    row_id: str
    readings: tuple[int | None, ...]
    provenance: tuple[ProvenanceSlice, ...]
    totals: LineTotals
    receipt: PublicationReceipt
    seal: SealRecord | None


@dataclass(frozen=True)
class HandoverDelivery:
    manifest: ShiftManifest
    lines: tuple[LineDelivery, ...]


def _line_delivery(publication: Publication, totals: LineTotals | None = None) -> LineDelivery:
    row = publication.row
    return LineDelivery(
        row.shift_id, row.line_id, row.row_id, row.readings, project_provenance(row),
        summarize_readings(row.readings) if totals is None else totals, publication.receipt, row.seal,
    )


def read(db: sqlite3.Connection, number: str) -> LineDelivery | HandoverDelivery:
    require_identifier(number, 'delivery number')
    if number.startswith('handover/'):
        issued = db.execute('SELECT 1 FROM handover_issues WHERE number = ?', (number,)).fetchone()
        if issued is None:
            raise LookupError(f'unknown or unsigned handover: {number}')
        manifest = get_manifest(db, number)
        lines = tuple(
            _line_delivery(get_publication(db, entry.publication_number), entry.totals)
            for entry in manifest.entries
        )
        return HandoverDelivery(manifest, lines)
    return _line_delivery(get_publication(db, number))
