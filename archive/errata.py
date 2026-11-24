"""测线勘误：以复测实收件为证据，对已签班单中的点段办理勘误。"""
import json
import sqlite3
from dataclasses import asdict, dataclass
from urllib.parse import quote

from archive.publication import get_publication
from intake.custody import load_receipted_fragment
from intake.fragments import ValidatedFragment
from review.manifest import get_manifest
from review.provenance import ProvenanceSlice
from review.totals import LineTotals, summarize_readings
from shift.lines import require_identifier
from shift.open_shift import station_write

ERRATA_SCHEMA = '''
CREATE TABLE IF NOT EXISTS errata_orders (
    number TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    base_number TEXT NOT NULL REFERENCES handover_issues(number),
    line_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    start INTEGER NOT NULL CHECK (start >= 0),
    stop INTEGER NOT NULL CHECK (stop > start),
    body TEXT NOT NULL,
    UNIQUE (shift_id, event_id)
);
CREATE TABLE IF NOT EXISTS errata_receipts (
    number TEXT PRIMARY KEY REFERENCES errata_orders(number),
    receipt_id TEXT NOT NULL UNIQUE
);
'''

REVISED_SCHEMA = '''
CREATE TABLE IF NOT EXISTS revised_manifests (
    number TEXT PRIMARY KEY REFERENCES handover_issues(number),
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    root_number TEXT NOT NULL,
    parent_number TEXT NOT NULL,
    base_number TEXT NOT NULL,
    revision INTEGER NOT NULL,
    totals TEXT NOT NULL,
    UNIQUE (root_number, revision)
);
CREATE TABLE IF NOT EXISTS revised_manifest_entries (
    number TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    line_id TEXT NOT NULL,
    publication_number TEXT NOT NULL,
    totals TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (number, ordinal),
    UNIQUE (number, line_id)
);
'''


class ErrataRejected(ValueError):
    """勘误审核未过，整单拒绝；错误原因放在 status。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class LineState:
    """某一版班单中一条测线的当前读值与逐段出处。"""
    shift_id: str
    line_id: str
    row_id: str
    readings: tuple[int | None, ...]
    provenance: tuple[ProvenanceSlice, ...]


@dataclass(frozen=True)
class ErrataOrder:
    number: str
    shift_id: str
    base_number: str
    line_id: str
    event_id: str
    start: int
    stop: int
    state: LineState
    receipt_id: str


def errata_number(shift_id: str, request_id: str) -> str:
    shift = quote(require_identifier(shift_id, 'shift identifier'), safe='')
    request = quote(require_identifier(request_id, 'errata request identifier'), safe='')
    return f'errata/{shift}/{request}'


def _slice_from_fragment(fragment: ValidatedFragment, start: int, stop: int) -> ProvenanceSlice:
    key = fragment.attempt.key
    return ProvenanceSlice(
        start, stop, key.recording_id, key.roll, key.slot, key.load,
        fragment.attempt.event_id, fragment.attempt.checksum,
    )


def project_correction(state: LineState, fragment: ValidatedFragment, start: int, stop: int) -> LineState:
    """只替换申报点段，未替换点保留原读值和出处；跨原换卷边界按点位办理。"""
    if (fragment.shift_id, fragment.line_id, fragment.row_id) != (state.shift_id, state.line_id, state.row_id):
        raise ErrataRejected('wrong_line')
    width = len(state.readings)
    if type(start) is not int or type(stop) is not int or start < 0 or stop <= start or stop > width:
        raise ErrataRejected('invalid_span')
    if fragment.total != width or fragment.origin.start > start or fragment.origin.stop < stop:
        raise ErrataRejected('span_not_covered')
    values = fragment.values[start - fragment.origin.start: stop - fragment.origin.start]
    readings = list(state.readings)
    readings[start:stop] = values
    new_origin = _slice_from_fragment(fragment, start, stop)
    provenance = replace_provenance(state.provenance, start, stop, new_origin)
    return LineState(state.shift_id, state.line_id, state.row_id, tuple(readings), provenance)


def replace_provenance(provenance, start: int, stop: int, new_origin: ProvenanceSlice) -> tuple:
    """按点段替换出处：与点段相交的原出处在边界处裁开，未替换点保留原实收。"""
    pieces = []
    for origin in provenance:
        if origin.stop <= start or origin.start >= stop:
            pieces.append(origin)
            continue
        if origin.start < start:
            pieces.append(ProvenanceSlice(
                origin.start, start, origin.recording_id, origin.roll, origin.slot,
                origin.load, origin.event_id, origin.checksum,
            ))
        if origin.stop > stop:
            pieces.append(ProvenanceSlice(
                stop, origin.stop, origin.recording_id, origin.roll, origin.slot,
                origin.load, origin.event_id, origin.checksum,
            ))
    pieces.append(new_origin)
    return tuple(sorted(pieces, key=lambda origin: origin.start))


def _revised_row(db: sqlite3.Connection, number: str):
    return db.execute(
        'SELECT shift_id, root_number, parent_number, base_number, revision FROM revised_manifests WHERE number = ?',
        (number,),
    ).fetchone()


def handover_root(db: sqlite3.Connection, number: str) -> str:
    row = _revised_row(db, number)
    return number if row is None else row['root_number']


def _decode_state(payload) -> LineState:
    body = json.loads(payload) if isinstance(payload, str) else payload
    provenance = tuple(ProvenanceSlice(**item) for item in body['provenance'])
    return LineState(body['shift_id'], body['line_id'], body['row_id'], tuple(body['readings']), provenance)


def _encode_state(state: LineState) -> str:
    return json.dumps({
        'shift_id': state.shift_id, 'line_id': state.line_id, 'row_id': state.row_id,
        'readings': list(state.readings), 'provenance': [asdict(item) for item in state.provenance],
    }, ensure_ascii=True)


def _root_line_state(db: sqlite3.Connection, number: str, line_id: str) -> LineState:
    manifest = get_manifest(db, number)
    entry = next((item for item in manifest.entries if item.line_id == line_id), None)
    if entry is None:
        raise ErrataRejected('line_not_in_handover')
    publication = get_publication(db, entry.publication_number)
    row = publication.row
    return LineState(
        row.shift_id, row.line_id, row.row_id, row.readings,
        tuple(
            ProvenanceSlice(
                origin.start, origin.stop, origin.key.recording_id, origin.key.roll,
                origin.key.slot, origin.key.load, origin.event_id, origin.checksum,
            )
            for origin in row.origins
        ),
    )


def effective_line_state(db: sqlite3.Connection, number: str, line_id: str) -> LineState:
    """领取某版班单中一条测线当前所依据的读值和出处。"""
    revised = _revised_row(db, number)
    if revised is None:
        return _root_line_state(db, number, line_id)
    entry = db.execute(
        'SELECT state FROM revised_manifest_entries WHERE number = ? AND line_id = ?',
        (number, line_id),
    ).fetchone()
    if entry is None:
        raise ErrataRejected('line_not_in_handover')
    return _decode_state(entry['state'])


def chain_revisions(db: sqlite3.Connection, number: str) -> tuple:
    root = handover_root(db, number)
    rows = db.execute(
        'SELECT number FROM revised_manifests WHERE root_number = ? ORDER BY revision', (root,)
    ).fetchall()
    return tuple(row['number'] for row in rows)


def is_ancestor(db: sqlite3.Connection, ancestor: str, descendant: str) -> bool:
    if ancestor == descendant:
        return True
    root = handover_root(db, descendant)
    if handover_root(db, ancestor) != root:
        return False
    return ancestor in chain_revisions(db, descendant) or ancestor == root


def _load_errata_row(db: sqlite3.Connection, number: str):
    row = db.execute('SELECT * FROM errata_orders WHERE number = ?', (number,)).fetchone()
    if row is None:
        return None
    receipt = db.execute(
        'SELECT receipt_id FROM errata_receipts WHERE number = ?', (number,)
    ).fetchone()
    body = json.loads(row['body'])
    state = _decode_state(body['state'])
    return ErrataOrder(
        row['number'], row['shift_id'], row['base_number'], row['line_id'], row['event_id'],
        row['start'], row['stop'], state, receipt['receipt_id'],
    )


def get_errata(db: sqlite3.Connection, number: str) -> ErrataOrder:
    order = _load_errata_row(db, number)
    if order is None:
        raise LookupError(f'unknown or unsigned errata: {number}')
    return order


def file_errata(
    db: sqlite3.Connection, shift_id: str, request_id: str, base_number: str,
    line_id: str, event_id: str, start: int, stop: int,
) -> ErrataOrder:
    """审核并固定一份勘误单。审核不过整单拒绝，不留任何记录。"""
    require_identifier(shift_id, 'shift identifier')
    require_identifier(base_number, 'handover number')
    require_identifier(line_id, 'line identifier')
    require_identifier(event_id, 'event identifier')
    number = errata_number(shift_id, request_id)
    with station_write(db):
        existing = _load_errata_row(db, number)
        params = (shift_id, base_number, line_id, event_id, start, stop)
        if existing is not None:
            existing_params = (
                existing.shift_id, existing.base_number, existing.line_id,
                existing.event_id, existing.start, existing.stop,
            )
            if existing_params != params:
                raise ValueError('errata number already has a different request')
            return existing
        issued = db.execute(
            'SELECT shift_id FROM handover_issues WHERE number = ?', (base_number,)
        ).fetchone()
        if issued is None:
            raise LookupError(f'errata must reference a signed handover: {base_number}')
        if issued['shift_id'] != shift_id:
            raise ErrataRejected('wrong_line')
        reused = db.execute(
            'SELECT number FROM errata_orders WHERE shift_id = ? AND event_id = ? AND number != ?',
            (shift_id, event_id, number),
        ).fetchone()
        if reused is not None:
            raise ValueError(f'retest evidence already backs errata {reused["number"]}')
        state = effective_line_state(db, base_number, line_id)
        try:
            fragment = load_receipted_fragment(db, shift_id, event_id)
        except LookupError as exc:
            raise ErrataRejected('unreceived_evidence') from exc
        if fragment.kind != 'retest':
            raise ErrataRejected('unreceived_evidence')
        if fragment.shift_id != shift_id or fragment.line_id != line_id:
            raise ErrataRejected('wrong_line')
        corrected = project_correction(state, fragment, start, stop)
        body = json.dumps({
            'state': json.loads(_encode_state(corrected)),
            'retest': {
                'event_id': fragment.attempt.event_id,
                'attempt': fragment.attempt.key.fields(),
                'checksum': fragment.attempt.checksum,
                'origin_start': fragment.origin.start,
                'origin_stop': fragment.origin.stop,
            },
        }, ensure_ascii=True)
        receipt_id = f'errata-receipt/{number}'
        db.execute(
            'INSERT INTO errata_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (number, shift_id, base_number, line_id, event_id, start, stop, body),
        )
        db.execute('INSERT INTO errata_receipts VALUES (?, ?)', (number, receipt_id))
        return ErrataOrder(number, shift_id, base_number, line_id, event_id, start, stop, corrected, receipt_id)


def state_totals(state: LineState) -> LineTotals:
    return summarize_readings(state.readings)
