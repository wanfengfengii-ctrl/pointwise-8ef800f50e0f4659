from dataclasses import dataclass

from archive.assembly import AssembledRow


@dataclass(frozen=True)
class ProvenanceSlice:
    start: int
    stop: int
    recording_id: str
    roll: str
    slot: int
    load: int
    event_id: str
    checksum: str


def project_provenance(row: AssembledRow) -> tuple[ProvenanceSlice, ...]:
    return tuple(
        ProvenanceSlice(
            origin.start, origin.stop, origin.key.recording_id, origin.key.roll,
            origin.key.slot, origin.key.load, origin.event_id, origin.checksum,
        )
        for origin in row.origins
    )
