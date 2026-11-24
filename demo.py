import argparse
import json
import tempfile
from contextlib import closing
from pathlib import Path

from archive.publication import PublicationBlocked, publication_number, publish
from device.recording import RecordingDevice
from handover.accept import accept
from handover.issue import finish_shift_handover, issue
from intake.capture import capture
from recording_sample import RECORDING, ROSTER, SHIFT, ordinary, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, open_shift


class RollRowMismatch(RuntimeError):
    pass


def emit(case_id, input_data, expected, actual, downstream_expected, downstream_actual, paths, oracle):
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id, 'input': input_data, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': paths, 'oracle': oracle,
    }, ensure_ascii=False, separators=(',', ':')))


def capture_next(db, device, expected_status):
    event = device.next_event()
    if event is None:
        raise RuntimeError('recording ended or stopped before its next expected event')
    result = capture(db, device, SHIFT, event)
    if result.status != expected_status:
        raise RuntimeError(f'unexpected capture status for {event.event_id}: {result.status}')
    return event


def run_rollover(db, device):
    originals = [capture_next(db, device, 'exited') for _ in range(3)]
    number = publication_number(SHIFT, 'bend')
    early = None
    try:
        publish(db, SHIFT, 'bend')
    except PublicationBlocked:
        pass
    else:
        early = list(read(db, number).readings)
    seal = capture_next(db, device, 'seal_received')
    try:
        signed = publish(db, SHIFT, 'bend')
    except PublicationBlocked as exc:
        raise RollRowMismatch(f'封卷后无成果，归档状态 {exc.status}') from exc
    result = read(db, signed.receipt.number)
    expected = {'before_seal': None, 'readings': [7, 13], 'origin_events': ['bend-head', 'bend-B']}
    actual = {
        'before_seal': early, 'readings': list(result.readings),
        'origin_events': [origin.event_id for origin in result.provenance],
    }
    totals = [result.totals.measured_count, result.totals.total_mm]
    origin_keys = [(item.recording_id, item.roll, item.slot, item.load) for item in result.provenance]
    if actual != expected or totals != [2, 20] or origin_keys != [
        (RECORDING, 'old-roll', 0, 2), (RECORDING, 'new-roll-B', 1, 2),
    ] or result.seal != seal:
        raise RollRowMismatch(f'期望 {expected}，实际 {actual}，出处 {origin_keys}')
    emit(
        'v4', {'fragments': [json.loads(item.raw) for item in originals], 'seal': json.loads(seal.encode())},
        expected, actual, [2, 20], totals,
        ['intake/capture.py', 'archive/eligibility.py', 'archive/assembly.py', 'review/lookup.py'],
        '封卷选 B，按两片的点位先放 7 再放 13，手算 2 点和 20 mm。',
    )


def run(directory: Path, rollover_only: bool):
    events = rollover_events()
    station_path = directory / 'station.sqlite'
    device = RecordingDevice(directory / 'device.sqlite', RECORDING, events if rollover_only else (ordinary(), *events))
    try:
        with closing(connect_station(station_path)) as db:
            open_shift(db, SHIFT, ROSTER)
            if not rollover_only:
                capture_next(db, device, 'exited')
                east = read(db, publish(db, SHIFT, 'east').receipt.number)
                if (east.readings, east.totals.measured_count, east.totals.total_mm) != ((4, None, 0), 2, 4):
                    raise RuntimeError('ordinary line readings or totals disagree with the recording')
            run_rollover(db, device)
            if rollover_only:
                return
            handover = issue(db, SHIFT, 'night-review', ('east', 'bend'))
            finish_shift_handover(db, SHIFT, handover.number)
    finally:
        device.close()

    with closing(connect_station(station_path)) as reopened:
        receipt = accept(reopened, handover.number)
        delivery = receipt.delivery
        actual = {line.line_id: list(line.readings) for line in delivery.lines}
        totals = [delivery.manifest.totals.measured_count, delivery.manifest.totals.total_mm]
        expected = {'bend': [7, 13], 'east': [4, None, 0]}
        if actual != expected or totals != [4, 24] or accept(reopened, handover.number) != receipt:
            raise RuntimeError('offline handover differs from the issued day results')
        emit(
            'v8', {'line_ids': ['east', 'bend'], 'handover_number': handover.number},
            expected, actual, [4, 24], totals,
            ['handover/issue.py', 'review/manifest.py', 'review/lookup.py', 'handover/accept.py'],
            '手算 bend 2 点 20 mm，east 2 点 4 mm，缺测原位保留，按班单只汇总这两条线。',
        )
        print(f'交班单 {handover.number}，接收首执 {receipt.receipt_id}')


def main():
    parser = argparse.ArgumentParser(description='演示班次录制和固定交班')
    parser.add_argument('--rollover', action='store_true', help='只核对指定续段的原行及出处')
    options = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='bridge-cape-') as directory:
            run(Path(directory), options.rollover)
    except RollRowMismatch as exc:
        parser.exit(1, f'ROLL_ROW_MISMATCH {exc}\n')


if __name__ == '__main__':
    main()
