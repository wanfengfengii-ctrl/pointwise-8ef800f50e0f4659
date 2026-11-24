from dataclasses import dataclass


@dataclass(frozen=True)
class LineTotals:
    measured_count: int
    total_mm: int


def summarize_readings(readings) -> LineTotals:
    measured = 0
    total = 0
    for value in readings:
        if value is not None:
            measured += 1
            total += value
    return LineTotals(measured, total)


def summarize_manifest(line_totals) -> LineTotals:
    measured = 0
    total = 0
    for line in line_totals:
        measured += line.measured_count
        total += line.total_mm
    return LineTotals(measured, total)
