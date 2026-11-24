import json
import sqlite3
from dataclasses import dataclass

from device.buffers import AttemptKey
from device.unload import query_unload
from shift.lines import require_identifier


@dataclass(frozen=True)
class SealRecord:
    recording_id: str
    event_id: str
    shift_id: str
    line_id: str
    row_id: str
    old_roll: str
    candidates: tuple[AttemptKey, ...]
    selected: AttemptKey | None

    def __post_init__(self):
        for field in ('recording_id', 'event_id', 'shift_id', 'line_id', 'row_id', 'old_roll'):
            require_identifier(getattr(self, field), field)
        if not isinstance(self.candidates, tuple) or len(self.candidates) > 2:
            raise ValueError('a seal can list at most two candidate attempts')
        for key in self.candidates:
            if not isinstance(key, AttemptKey) or key.recording_id != self.recording_id or key.roll == self.old_roll:
                raise ValueError('candidate outside the seal recording or on the old roll')
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError('duplicate candidate attempt in seal')
        if self.selected is not None and self.selected not in self.candidates:
            raise ValueError('the selected attempt must be listed in the seal')

    def encode(self) -> str:
        return json.dumps({
            'recording_id': self.recording_id, 'event_id': self.event_id, 'shift_id': self.shift_id,
            'line_id': self.line_id, 'row_id': self.row_id, 'old_roll': self.old_roll,
            'candidates': [key.fields() for key in self.candidates],
            'selected': self.selected.fields() if self.selected is not None else None,
        }, ensure_ascii=True, separators=(',', ':'))

    @classmethod
    def decode(cls, text: str) -> 'SealRecord':
        fields = json.loads(text)
        fields['candidates'] = tuple(AttemptKey(*key) for key in fields['candidates'])
        if fields['selected'] is not None:
            fields['selected'] = AttemptKey(*fields['selected'])
        return cls(**fields)


def expose_seal(db: sqlite3.Connection, record: SealRecord) -> SealRecord | None:
    if all(query_unload(db, key).status == 'exited' for key in record.candidates):
        return record
    return None
