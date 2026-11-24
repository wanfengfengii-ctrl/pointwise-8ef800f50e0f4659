import hashlib
import json

from device.buffers import AttemptKey, BufferAttempt
from device.seal import SealRecord

SHIFT = 'watch-1'
RECORDING = 'bridge-cape-1'
ROSTER = ('bend', 'east', 'spur')


def fragment(key, event_id, line_id, row_id, kind, start, total, values, *, shift_id=SHIFT):
    raw = json.dumps({
        'shift': shift_id, 'line': line_id, 'row': row_id, 'event': event_id,
        'attempt': key.fields(), 'kind': kind, 'start': start, 'total': total, 'values': values,
    }, separators=(',', ':')).encode('utf-8')
    return BufferAttempt(key, event_id, raw, hashlib.sha256(raw).hexdigest())


def ordinary():
    key = AttemptKey(RECORDING, 'lead-roll', 0, 1)
    return fragment(key, 'east-whole', 'east', 'east-row', 'whole', 0, 3, [4, None, 0])


def spur():
    key = AttemptKey(RECORDING, 'spur-roll', 0, 3)
    return fragment(key, 'spur-whole', 'spur', 'spur-row', 'whole', 0, 1, [5])


def retest(key, event_id, line_id, row_id, start, total, values, *, shift_id=SHIFT):
    return fragment(key, event_id, line_id, row_id, 'retest', start, total, values, shift_id=shift_id)


def rollover_events(*, selected='B', order=('A', 'B'), b_value=13, total=2, candidate_start=1):
    old_key = AttemptKey(RECORDING, 'old-roll', 0, 2)
    head = fragment(old_key, 'bend-head', 'bend', 'bend-row', 'head', 0, total, [7])
    candidates = {}
    for load, label in enumerate(order, start=1):
        key = AttemptKey(RECORDING, f'new-roll-{label}', 1, load)
        value = 11 if label == 'A' else b_value
        candidates[label] = fragment(
            key, f'bend-{label}', 'bend', 'bend-row', 'candidate', candidate_start, total, [value]
        )
    keys = tuple(candidates[label].key for label in ('A', 'B'))
    winner = candidates[selected].key if selected is not None else None
    seal = SealRecord(RECORDING, 'bend-seal', SHIFT, 'bend', 'bend-row', old_key.roll, keys, winner)
    return head, *candidates.values(), seal
