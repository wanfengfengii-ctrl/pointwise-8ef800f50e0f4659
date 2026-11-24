import json
from dataclasses import dataclass

from shift.lines import LineRoster, parse_line_roster, require_identifier

MM_UNIT = 'mm'


@dataclass(frozen=True)
class ShiftConfig:
    shift_id: str
    roster: LineRoster
    unit: str

    def encode(self) -> str:
        return json.dumps([self.shift_id, self.roster.line_ids, self.unit], ensure_ascii=True)

    @classmethod
    def decode(cls, text: str) -> 'ShiftConfig':
        shift_id, line_ids, unit = json.loads(text)
        return build_shift_config(shift_id, line_ids, unit)


def build_shift_config(shift_id: str, line_ids, unit: str = MM_UNIT) -> ShiftConfig:
    require_identifier(shift_id, 'shift identifier')
    if unit != MM_UNIT:
        raise ValueError('the recording unit must be integer millimetres')
    return ShiftConfig(shift_id, parse_line_roster(line_ids), unit)


def same_shift_config(left: ShiftConfig, right: ShiftConfig) -> bool:
    return (left.shift_id, left.roster, left.unit) == (right.shift_id, right.roster, right.unit)
