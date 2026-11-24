from dataclasses import dataclass


def require_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or not value.isprintable():
        raise ValueError(f'invalid {field}')
    return value


@dataclass(frozen=True)
class LineRoster:
    line_ids: tuple[str, ...]


def parse_line_roster(line_ids) -> LineRoster:
    if isinstance(line_ids, (str, bytes)):
        raise ValueError('a roster requires a collection of line identifiers')
    try:
        lines = tuple(require_identifier(line, 'line identifier') for line in line_ids)
    except TypeError as exc:
        raise ValueError('invalid line collection') from exc
    if not lines or len(lines) != len(set(lines)):
        raise ValueError('a roster must be nonempty and contain unique lines')
    return LineRoster(tuple(sorted(lines)))


def require_line(roster: LineRoster, line_id: str) -> str:
    require_identifier(line_id, 'line identifier')
    if line_id not in roster.line_ids:
        raise ValueError(f'line outside the shift roster: {line_id}')
    return line_id
