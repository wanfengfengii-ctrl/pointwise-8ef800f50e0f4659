import hashlib
import json
from dataclasses import dataclass

from device.buffers import AttemptKey, BufferAttempt
from shift.lines import require_identifier


@dataclass(frozen=True)
class FragmentOrigin:
    key: AttemptKey
    event_id: str
    start: int
    stop: int
    checksum: str


@dataclass(frozen=True)
class ValidatedFragment:
    attempt: BufferAttempt
    shift_id: str
    line_id: str
    row_id: str
    kind: str
    total: int
    values: tuple[int | None, ...]
    origin: FragmentOrigin


def _unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f'duplicate fragment field: {key}')
        fields[key] = value
    return fields


def validate_fragment(attempt: BufferAttempt) -> ValidatedFragment:
    if not isinstance(attempt, BufferAttempt):
        raise ValueError('expected a device buffer attempt')
    if hashlib.sha256(attempt.raw).hexdigest() != attempt.checksum:
        raise ValueError('fragment checksum mismatch')
    try:
        fields = json.loads(attempt.raw.decode('utf-8'), object_pairs_hook=_unique_fields)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid fragment JSON') from exc
    required = {'shift', 'line', 'row', 'event', 'attempt', 'kind', 'start', 'total', 'values'}
    if not isinstance(fields, dict) or fields.keys() != required:
        raise ValueError('invalid fragment header fields')
    physical = fields['attempt']
    if not isinstance(physical, list) or len(physical) != 4:
        raise ValueError('invalid physical reference in fragment')
    if AttemptKey(*physical) != attempt.key or fields['event'] != attempt.event_id:
        raise ValueError('fragment header disagrees with the buffer envelope')
    shift_id = require_identifier(fields['shift'], 'fragment shift')
    line_id = require_identifier(fields['line'], 'fragment line')
    row_id = require_identifier(fields['row'], 'fragment row')
    kind = fields['kind']
    if kind not in ('whole', 'head', 'candidate', 'retest'):
        raise ValueError('invalid fragment kind')
    start, total, values = fields['start'], fields['total'], fields['values']
    if type(start) is not int or type(total) is not int or total < 1 or start < 0:
        raise ValueError('invalid fragment point range')
    if not isinstance(values, list) or not values or any(value is not None and type(value) is not int for value in values):
        raise ValueError('readings must be a nonempty list of integers or null')
    stop = start + len(values)
    if stop > total:
        raise ValueError('fragment point range exceeds its declared row width')
    if kind == 'head' and (start != 0 or stop == total):
        raise ValueError('old-roll head must be a proper row prefix')
    origin = FragmentOrigin(attempt.key, attempt.event_id, start, stop, attempt.checksum)
    return ValidatedFragment(attempt, shift_id, line_id, row_id, kind, total, tuple(values), origin)
