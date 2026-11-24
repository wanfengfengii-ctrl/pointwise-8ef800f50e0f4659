import sqlite3
from dataclasses import dataclass

from archive.staging import StagedFragment, list_staged_fragments
from device.seal import SealRecord
from intake.custody import find_received_seal


@dataclass(frozen=True)
class PublicationEligibility:
    status: str
    fragments: tuple[StagedFragment, ...] = ()
    seal: SealRecord | None = None


def check_eligibility(db: sqlite3.Connection, shift_id: str, line_id: str) -> PublicationEligibility:
    staged = list_staged_fragments(db, shift_id, line_id)
    anchor = next((item for item in staged if item.kind in ('whole', 'head')), None)
    if anchor is None:
        return PublicationEligibility('missing_anchor')
    if anchor.kind == 'whole':
        return PublicationEligibility('ready', (anchor,))

    seal = find_received_seal(db, shift_id, line_id, anchor.row_id)
    if seal is None:
        return PublicationEligibility('awaiting_seal')
    if (seal.recording_id, seal.old_roll) != (anchor.key.recording_id, anchor.key.roll):
        return PublicationEligibility('wrong_seal')
    if seal.selected is None:
        return PublicationEligibility('no_winner', seal=seal)

    candidate = next((item for item in staged if item.key == seal.selected), None)
    if candidate is None:
        return PublicationEligibility('missing_selected', seal=seal)
    if candidate.kind != 'candidate' or candidate.row_id != anchor.row_id:
        return PublicationEligibility('wrong_selected', seal=seal)
    return PublicationEligibility('ready', (anchor, candidate), seal)
