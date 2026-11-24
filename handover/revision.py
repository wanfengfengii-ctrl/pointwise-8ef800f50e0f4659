"""班单改签：同一班单沿版本链生效，勘误成功后签出新班单和首执。"""
import json
import sqlite3
from dataclasses import dataclass

from archive.errata import (
    ERRATA_SCHEMA, REVISED_SCHEMA, LineState, _encode_state, effective_line_state,
    get_errata, handover_root, is_ancestor, replace_provenance, state_totals,
)
from archive.publication import publication_number
from review.manifest import get_manifest
from review.provenance import ProvenanceSlice
from review.totals import LineTotals, summarize_manifest
from shift.lines import require_identifier
from shift.open_shift import station_write
from handover.issue import handover_number

REISSUE_SCHEMA = '''
''' + ERRATA_SCHEMA + '''
''' + REVISED_SCHEMA + '''
CREATE TABLE IF NOT EXISTS reissue_errata (
    number TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    errata_number TEXT NOT NULL,
    PRIMARY KEY (number, ordinal),
    UNIQUE (number, errata_number)
);'''


class ReissueConflict(LookupError):
    """目标点段已有新复测出处（哪怕读值一致），或同批勘误点段相交。整单不签。"""

    def __init__(self, conflicts):
        super().__init__(
            'reissue conflict: ' + json.dumps(
                [asdict_conflict(item) for item in conflicts], ensure_ascii=True
            )
        )
        self.conflicts = tuple(conflicts)


@dataclass(frozen=True)
class ConflictDetail:
    line_id: str
    start: int
    stop: int
    current_origin: ProvenanceSlice
    incoming_origin: ProvenanceSlice


def asdict_conflict(conflict: ConflictDetail) -> dict:
    def origin_dict(origin):
        return {
            'event_id': origin.event_id,
            'attempt': [origin.recording_id, origin.roll, origin.slot, origin.load],
            'checksum': origin.checksum,
        }

    return {
        'line_id': conflict.line_id, 'start': conflict.start, 'stop': conflict.stop,
        'current_origin': origin_dict(conflict.current_origin),
        'incoming_origin': origin_dict(conflict.incoming_origin),
    }


@dataclass(frozen=True)
class Reissue:
    number: str
    root_number: str
    parent_number: str
    base_numbers: tuple[str, ...]
    errata_numbers: tuple[str, ...]
    revision: int
    states: tuple[LineState, ...]
    totals: LineTotals


def reissue_number(request_id: str) -> str:
    return handover_number(request_id)


def _head_number(db: sqlite3.Connection, root: str) -> str:
    row = db.execute(
        'SELECT number FROM revised_manifests WHERE root_number = ? ORDER BY revision DESC LIMIT 1', (root,)
    ).fetchone()
    return root if row is None else row['number']


def _introduced_events(db: sqlite3.Connection, base: str, head: str, line_id: str) -> frozenset[str]:
    """base 版以来该测线上被换过的复测出处事件。"""
    if base == head:
        return frozenset()
    base_events = {origin.event_id for origin in effective_line_state(db, base, line_id).provenance}
    head_state = effective_line_state(db, head, line_id)
    return frozenset(
        origin.event_id for origin in head_state.provenance if origin.event_id not in base_events
    )


def _conflicts_for_order(db, head, order, taken_spans):
    conflicts = []
    if not is_ancestor(db, order.base_number, head):
        raise ValueError('errata base is not on this handover version chain')
    head_state = effective_line_state(db, head, order.line_id)
    incoming = next(origin for origin in order.state.provenance if origin.event_id == order.event_id)
    introduced = _introduced_events(db, order.base_number, head, order.line_id)
    for position in range(order.start, order.stop):
        current = next(
            (origin for origin in head_state.provenance
             if origin.start <= position < origin.stop and origin.event_id in introduced),
            None,
        )
        if current is not None:
            conflicts.append(ConflictDetail(order.line_id, order.start, order.stop, current, incoming))
            break
    for other_start, other_stop, other_origin in taken_spans.get(order.line_id, ()):
        if order.start < other_stop and other_start < order.stop:
            conflicts.append(ConflictDetail(order.line_id, order.start, order.stop, other_origin, incoming))
    return conflicts


def _decode_state(payload: str) -> LineState:
    body = json.loads(payload)
    return LineState(
        body['shift_id'], body['line_id'], body['row_id'], tuple(body['readings']),
        tuple(ProvenanceSlice(**origin) for origin in body['provenance']),
    )


def get_reissue(db: sqlite3.Connection, number: str) -> Reissue:
    row = db.execute(
        'SELECT root_number, parent_number, revision, totals FROM revised_manifests WHERE number = ?',
        (number,),
    ).fetchone()
    if row is None:
        raise LookupError(f'unknown or unsigned reissue: {number}')
    entries = db.execute(
        'SELECT state FROM revised_manifest_entries WHERE number = ? ORDER BY ordinal', (number,)
    ).fetchall()
    states = tuple(_decode_state(item['state']) for item in entries)
    errata = db.execute(
        'SELECT errata_number FROM reissue_errata WHERE number = ? ORDER BY ordinal', (number,)
    ).fetchall()
    errata_numbers = tuple(item['errata_number'] for item in errata)
    base_numbers = tuple(get_errata(db, item).base_number for item in errata_numbers)
    return Reissue(
        number, row['root_number'], row['parent_number'], base_numbers,
        errata_numbers, row['revision'], states, LineTotals(**json.loads(row['totals'])),
    )


def _manifest_totals_dict(totals: LineTotals) -> str:
    return json.dumps({'measured_count': totals.measured_count, 'total_mm': totals.total_mm})


def reissue(db: sqlite3.Connection, shift_id: str, request_id: str, root_number: str, errata_numbers) -> Reissue:
    """多测线勘误同次签出；任一处冲突整单回滚，当前班单及原首执保持不变。"""
    require_identifier(shift_id, 'shift identifier')
    require_identifier(root_number, 'root handover number')
    numbers = tuple(errata_numbers)
    if not numbers or len(set(numbers)) != len(numbers):
        raise ValueError('a reissue needs a nonempty set of distinct errata orders')
    number = reissue_number(request_id)
    with station_write(db):
        if db.execute('SELECT 1 FROM handover_issues WHERE number = ?', (number,)).fetchone() is not None:
            previous = get_reissue(db, number)
            if (root_number, previous.errata_numbers) != (previous.root_number, numbers):
                raise ValueError('reissue number already has a different request')
            return previous
        root_row = db.execute(
            'SELECT shift_id FROM handover_issues WHERE number = ?', (root_number,)
        ).fetchone()
        if root_row is None:
            raise LookupError(f'unknown root handover: {root_number}')
        if root_row['shift_id'] != shift_id:
            raise ValueError('reissue belongs to another shift')
        if handover_root(db, root_number) != root_number:
            raise ValueError('reissue must reference the root handover of a version chain')
        placeholders = ','.join('?' for _ in numbers)
        already_signed = db.execute(
            f'SELECT errata_number, number FROM reissue_errata WHERE errata_number IN ({placeholders})',
            numbers,
        ).fetchall()
        if already_signed:
            row = already_signed[0]
            raise ValueError(
                f'errata {row["errata_number"]} is already signed into {row["number"]}'
            )
        head = _head_number(db, root_number)
        orders = tuple(get_errata(db, item) for item in numbers)
        root_lines = tuple(entry.line_id for entry in get_manifest(db, root_number).entries)
        for order in orders:
            if order.shift_id != shift_id or order.line_id not in root_lines:
                raise ValueError('errata references a line outside the root handover')
        conflicts = []
        taken_spans = {}
        for order in orders:
            conflicts.extend(_conflicts_for_order(db, head, order, taken_spans))
            incoming = next(origin for origin in order.state.provenance if origin.event_id == order.event_id)
            taken_spans.setdefault(order.line_id, []).append((order.start, order.stop, incoming))
        if conflicts:
            raise ReissueConflict(conflicts)

        states = {line_id: effective_line_state(db, head, line_id) for line_id in root_lines}
        for order in orders:
            head_state = states[order.line_id]
            new_slice = next(origin for origin in order.state.provenance if origin.event_id == order.event_id)
            readings = list(head_state.readings)
            readings[order.start:order.stop] = order.state.readings[order.start:order.stop]
            provenance = replace_provenance(head_state.provenance, order.start, order.stop, new_slice)
            states[order.line_id] = LineState(
                head_state.shift_id, head_state.line_id, head_state.row_id, tuple(readings), provenance,
            )
        ordered_states = tuple(states[line_id] for line_id in root_lines)
        line_totals = tuple(state_totals(state) for state in ordered_states)
        totals = summarize_manifest(line_totals)
        revision = db.execute(
            'SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM revised_manifests WHERE root_number = ?',
            (root_number,),
        ).fetchone()['revision']
        db.execute(
            'INSERT INTO manifests VALUES (?, ?, ?)', (number, shift_id, _manifest_totals_dict(totals))
        )
        db.executemany(
            'INSERT INTO manifest_entries VALUES (?, ?, ?, ?, ?)',
            (
                (number, ordinal, state.line_id, publication_number(shift_id, state.line_id),
                 _manifest_totals_dict(line_totals[ordinal]))
                for ordinal, state in enumerate(ordered_states)
            ),
        )
        db.execute('INSERT INTO handover_issues VALUES (?, ?)', (number, shift_id))
        db.execute(
            '''INSERT INTO revised_manifests
               (number, shift_id, root_number, parent_number, base_number, revision, totals)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (number, shift_id, root_number, head, head, revision, _manifest_totals_dict(totals)),
        )
        db.executemany(
            '''INSERT INTO revised_manifest_entries
               (number, ordinal, line_id, publication_number, totals, state)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                (
                    number, ordinal, state.line_id, publication_number(shift_id, state.line_id),
                    _manifest_totals_dict(line_totals[ordinal]), _encode_state(state),
                )
                for ordinal, state in enumerate(ordered_states)
            ),
        )
        db.executemany(
            'INSERT INTO reissue_errata VALUES (?, ?, ?)',
            ((number, ordinal, item) for ordinal, item in enumerate(numbers)),
        )
        base_numbers = tuple(order.base_number for order in orders)
        return Reissue(number, root_number, head, base_numbers, numbers, revision, ordered_states, totals)
