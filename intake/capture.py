import sqlite3
from dataclasses import dataclass

from archive.staging import stage_fragment
from device.buffers import BufferAttempt
from device.recording import RecordingDevice
from device.seal import SealRecord
from intake.custody import (
    CaptureReceipt, get_capture_receipt, load_received_seal, store_fragment, store_received_seal,
)
from intake.fragments import validate_fragment
from intake.resume import resume
from shift.close_shift import get_closure
from shift.lines import require_line
from shift.open_shift import get_shift, station_write


@dataclass(frozen=True)
class CaptureResult:
    status: str
    receipt: CaptureReceipt | None = None
    seal: SealRecord | None = None


def _require_intake_open(db, shift_id, already_received):
    if not already_received and get_closure(db, shift_id) is not None:
        raise ValueError('shift recording has already been closed')


def capture(
    db: sqlite3.Connection, device: RecordingDevice | None, shift_id: str, event: BufferAttempt | SealRecord
) -> CaptureResult:
    if isinstance(event, SealRecord):
        if event.shift_id != shift_id or (device is not None and event.recording_id != device.recording_id):
            raise ValueError('seal belongs to another shift or recording')
        with station_write(db):
            require_line(get_shift(db, shift_id).roster, event.line_id)
            existing = load_received_seal(db, shift_id, event.event_id)
            _require_intake_open(db, shift_id, existing is not None)
            stored = store_received_seal(db, event)
        return CaptureResult('seal_received', seal=stored)

    fragment = validate_fragment(event)
    if fragment.shift_id != shift_id or (device is not None and event.key.recording_id != device.recording_id):
        raise ValueError('fragment belongs to another shift or recording')
    with station_write(db):
        require_line(get_shift(db, shift_id).roster, fragment.line_id)
        previous = get_capture_receipt(db, shift_id, event.event_id)
        _require_intake_open(db, shift_id, previous is not None)
        receipt = store_fragment(db, fragment)
        stage_fragment(db, receipt.shift_id, receipt.event_id)
    result = resume(db, shift_id, receipt.event_id, device)
    return CaptureResult(result.status, receipt=result.receipt)
