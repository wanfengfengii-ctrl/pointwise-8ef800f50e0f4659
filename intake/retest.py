"""复测件的采录：只入实收，不进归档，作为勘误引用的实收证据。"""
import sqlite3
from dataclasses import dataclass

from device.buffers import BufferAttempt
from device.recording import RecordingDevice
from intake.custody import CaptureReceipt, get_capture_receipt, store_fragment
from intake.fragments import ValidatedFragment, validate_fragment
from intake.resume import resume
from shift.lines import require_line
from shift.open_shift import get_shift, station_write


@dataclass(frozen=True)
class RetestReceipt:
    receipt: CaptureReceipt
    fragment: ValidatedFragment


def _store_retest(db: sqlite3.Connection, fragment: ValidatedFragment) -> CaptureReceipt:
    if fragment.kind != 'retest':
        raise ValueError('a retest piece must use the retest fragment kind')
    require_line(get_shift(db, fragment.shift_id).roster, fragment.line_id)
    previous = get_capture_receipt(db, fragment.shift_id, fragment.attempt.event_id)
    if previous is not None:
        if (previous.key, previous.checksum) != (fragment.attempt.key, fragment.attempt.checksum):
            raise ValueError('retest event replay has different content')
        return previous
    return store_fragment(db, fragment)


def receive_retest(
    db: sqlite3.Connection, device: RecordingDevice | None, shift_id: str, event: BufferAttempt
) -> RetestReceipt:
    """采录复测件。复测件属于勘误业务，班单终结后仍可续办，不进入发布归档。"""
    fragment = validate_fragment(event)
    if fragment.shift_id != shift_id or (device is not None and event.key.recording_id != device.recording_id):
        raise ValueError('retest piece belongs to another shift or recording')
    with station_write(db):
        receipt = _store_retest(db, fragment)
    result = resume(db, shift_id, receipt.event_id, device)
    return RetestReceipt(result.receipt, fragment)
