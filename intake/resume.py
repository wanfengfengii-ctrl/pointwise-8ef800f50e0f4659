import sqlite3
from dataclasses import dataclass

from device.recording import RecordingDevice
from device.seal import SealRecord
from device.unload import DeviceUnavailable, UnloadOutcome
from intake.custody import CaptureReceipt, get_capture_receipt, load_received_seal, record_exit_status
from shift.lines import require_identifier


@dataclass(frozen=True)
class ResumeResult:
    status: str
    receipt: CaptureReceipt | None = None
    seal: SealRecord | None = None


def _observe_or_exit(device: RecordingDevice, receipt: CaptureReceipt) -> UnloadOutcome | None:
    try:
        outcome = device.query_unload(receipt.key)
        if (outcome.key, outcome.event_id) != (receipt.key, receipt.event_id):
            return None
        if outcome.status == 'resident':
            return device.unload(receipt.key)
        return outcome
    except (DeviceUnavailable, LookupError):
        return None


def resume(db: sqlite3.Connection, shift_id: str, event_id: str, device: RecordingDevice | None = None) -> ResumeResult:
    require_identifier(event_id, 'event identifier')
    seal = load_received_seal(db, shift_id, event_id)
    if seal is not None:
        return ResumeResult('seal_received', seal=seal)
    receipt = get_capture_receipt(db, shift_id, event_id)
    if receipt is None:
        raise LookupError(f'no custody for event {event_id}')
    if receipt.exit_status != 'exited':
        outcome = _observe_or_exit(device, receipt) if device is not None else None
        receipt = record_exit_status(db, receipt, outcome)
    return ResumeResult(receipt.exit_status, receipt=receipt)
