import sqlite3
from dataclasses import dataclass

from archive.staging import StagedFragment
from device.seal import SealRecord
from intake.custody import load_receipted_fragment
from intake.fragments import FragmentOrigin, ValidatedFragment


class InvalidSpan(ValueError):
    pass


@dataclass(frozen=True)
class AssembledRow:
    shift_id: str
    line_id: str
    row_id: str
    readings: tuple[int | None, ...]
    origins: tuple[FragmentOrigin, ...]
    seal: SealRecord | None


def _load_staged(db: sqlite3.Connection, item: StagedFragment) -> ValidatedFragment:
    try:
        fragment = load_receipted_fragment(db, item.shift_id, item.event_id)
    except (ValueError, LookupError) as exc:
        raise InvalidSpan('staged fragment has no verifiable original') from exc
    if (fragment.attempt.key, fragment.line_id, fragment.row_id, fragment.kind) != (
        item.key, item.line_id, item.row_id, item.kind
    ):
        raise InvalidSpan('staged fragment disagrees with its original')
    return fragment


def assemble_row(
    db: sqlite3.Connection, staged: tuple[StagedFragment, ...], seal: SealRecord | None = None
) -> AssembledRow:
    parts = sorted((_load_staged(db, item) for item in staged), key=lambda part: part.origin.start)
    kinds = tuple(part.kind for part in parts)
    if kinds not in (('whole',), ('head', 'candidate')):
        raise InvalidSpan('expected a whole fragment or a head followed by a candidate')
    first = parts[0]
    identity = (first.shift_id, first.line_id, first.row_id, first.attempt.key.recording_id, first.total)
    if len(parts) == 2 and first.attempt.key.roll == parts[1].attempt.key.roll:
        raise InvalidSpan('rollover fragments must come from two rolls')
    readings = []
    origins = []
    for part in parts:
        if (part.shift_id, part.line_id, part.row_id, part.attempt.key.recording_id, part.total) != identity:
            raise InvalidSpan('fragments disagree on row identity, recording or row width')
        if part.origin.start != len(readings):
            raise InvalidSpan('fragment intervals overlap or leave a gap')
        readings.extend(part.values)
        origins.append(part.origin)
    if len(readings) != first.total:
        raise InvalidSpan('fragment intervals do not cover the complete row')
    return AssembledRow(first.shift_id, first.line_id, first.row_id, tuple(readings), tuple(origins), seal)
