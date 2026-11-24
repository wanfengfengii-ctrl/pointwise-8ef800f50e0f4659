import argparse
import json
import tempfile
from contextlib import closing
from pathlib import Path

from archive.errata import ErrataRejected, file_errata
from archive.publication import PublicationBlocked, publication_number, publish
from device.buffers import AttemptKey
from device.recording import RecordingDevice
from handover.accept import accept
from handover.issue import finish_shift_handover, issue
from handover.revision import ReissueConflict, get_reissue, reissue
from intake.capture import capture
from intake.retest import receive_retest
from recording_sample import RECORDING, ROSTER, SHIFT, ordinary, retest, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, open_shift

RETEST_RECORDING = 'bridge-cape-retest'


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


def retest_piece(event_id, line_id, row_id, start, total, values):
    key = AttemptKey(RETEST_RECORDING, f'roll-{event_id}', 0, 1)
    return retest(key, event_id, line_id, row_id, start, total, values)


def receive_piece(db, directory: Path, piece):
    device = RecordingDevice(directory / f'{piece.event_id}.sqlite', RETEST_RECORDING, (piece,))
    try:
        result = receive_retest(db, device, SHIFT, device.next_event())
        if result.receipt.exit_status != 'exited':
            raise RollRowMismatch(f'复测件 {piece.event_id} 未取得实收：{result.receipt.exit_status}')
    finally:
        device.close()
    return piece


def line_view(delivery, line_id):
    return next(line for line in delivery.lines if line.line_id == line_id)


def origin_events(delivery, line_id):
    return [(item.start, item.stop, item.event_id) for item in line_view(delivery, line_id).provenance]


def run_errata(db, directory: Path, root_number: str):
    # 场景一：等值异出处。复测读值与原读值同为 4，但出处必须换成复测实收。
    equal_piece = receive_piece(db, directory, retest_piece('rt-east-eq', 'east', 'east-row', 0, 3, [4]))
    equal_order = file_errata(db, SHIFT, 'err-eq', root_number, 'east', equal_piece.event_id, 0, 1)
    rev_equal = reissue(db, SHIFT, 'night-review-r1', root_number, (equal_order.number,))
    delivery = read(db, rev_equal.number)
    actual = {
        'readings': list(line_view(delivery, 'east').readings),
        'origin_events': origin_events(delivery, 'east'),
    }
    expected = {'readings': [4, None, 0], 'origin_events': [(0, 1, 'rt-east-eq'), (1, 3, 'east-whole')]}
    if actual != expected:
        raise RollRowMismatch(f'等值异出处结果不符：{actual}')
    emit(
        'v9', {'event_id': 'rt-east-eq', 'span': [0, 1], 'base': root_number},
        expected, actual, [4, 24],
        [delivery.manifest.totals.measured_count, delivery.manifest.totals.total_mm],
        ['intake/retest.py', 'archive/errata.py', 'handover/revision.py', 'review/lookup.py'],
        '读值同为 4 也按复测出处替换 0 号点，1..2 号点保留 east-whole 原读值和出处，班账仍为 4 点 24 mm。',
    )
    print(f'改签一 {rev_equal.number}（版本 {rev_equal.revision}，父版 {rev_equal.parent_number}）')

    # 等值再报冲突：新旧读值一致也必须报告，整单不签，当前班单和原首执不动。
    again_piece = receive_piece(db, directory, retest_piece('rt-east-eq-again', 'east', 'east-row', 0, 3, [4]))
    again_order = file_errata(db, SHIFT, 'err-eq-again', root_number, 'east', again_piece.event_id, 0, 1)
    try:
        reissue(db, SHIFT, 'night-review-conflict', root_number, (again_order.number,))
    except ReissueConflict as exc:
        detail = exc.conflicts[0]
        conflict_actual = {
            'line_id': detail.line_id, 'span': [detail.start, detail.stop],
            'current_origin': detail.current_origin.event_id,
            'incoming_origin': detail.incoming_origin.event_id,
        }
    else:
        raise RollRowMismatch('等值异出处复测未报冲突')
    conflict_expected = {
        'line_id': 'east', 'span': [0, 1],
        'current_origin': 'rt-east-eq', 'incoming_origin': 'rt-east-eq-again',
    }
    if conflict_actual != conflict_expected:
        raise RollRowMismatch(f'冲突依据不符：{conflict_actual}')
    emit(
        'v9b', {'event_id': 'rt-east-eq-again', 'span': [0, 1], 'base': root_number},
        conflict_expected, conflict_actual, None, None,
        ['handover/revision.py'],
        '目标点已有新复测出处 rt-east-eq，即使两份读值都是 4 也冲突，返回受影响测线、点位和两份依据。',
    )

    # 场景二：两份引用同一旧版（原班单）的独立复测，点段互不相交且含相邻缺测段。
    east_two = receive_piece(db, directory, retest_piece('rt-east-two', 'east', 'east-row', 2, 3, [9]))
    east_missing = receive_piece(db, directory, retest_piece('rt-east-missing', 'east', 'east-row', 1, 3, [None]))
    order_two = file_errata(db, SHIFT, 'err-east-two', root_number, 'east', east_two.event_id, 2, 3)
    order_missing = file_errata(db, SHIFT, 'err-east-missing', root_number, 'east', east_missing.event_id, 1, 2)
    rev_disjoint = reissue(
        db, SHIFT, 'night-review-r2', root_number, (order_two.number, order_missing.number)
    )
    delivery = read(db, rev_disjoint.number)
    actual = {
        'east': {'readings': list(line_view(delivery, 'east').readings),
                 'origin_events': origin_events(delivery, 'east')},
        'bend': {'readings': list(line_view(delivery, 'bend').readings),
                 'origin_events': origin_events(delivery, 'bend')},
    }
    expected = {
        'east': {'readings': [4, None, 9], 'origin_events': [
            (0, 1, 'rt-east-eq'), (1, 2, 'rt-east-missing'), (2, 3, 'rt-east-two')]},
        'bend': {'readings': [7, 13], 'origin_events': [(0, 1, 'bend-head'), (1, 2, 'bend-B')]},
    }
    if actual != expected:
        raise RollRowMismatch(f'不相交勘误结果不符：{actual}')
    emit(
        'v10', {'orders': ['err-east-two', 'err-east-missing'], 'base': root_number},
        expected, actual, [4, 33],
        [delivery.manifest.totals.measured_count, delivery.manifest.totals.total_mm],
        ['handover/revision.py', 'review/manifest.py'],
        '两份勘误都引用原班单；east 1 号点明确缺测原位保留，相邻点段互不阻塞；bend 未在本批，原换卷两处出处原样保留。手算 bend 20、east 13，共 4 点 33 mm。',
    )
    print(f'改签二 {rev_disjoint.number}（版本 {rev_disjoint.revision}，父版 {rev_disjoint.parent_number}）')

    # 场景四：多线同次签出时一处冲突，整单拒绝并返回受影响测线、点位和两份依据。
    # 此时 bend 0 号点仍是原 bend-head 出处（不冲突），east 0 号点已换复测出处（冲突）。
    clash_piece = receive_piece(db, directory, retest_piece('rt-east-clash', 'east', 'east-row', 0, 3, [8]))
    clean_piece = receive_piece(db, directory, retest_piece('rt-bend-clean', 'bend', 'bend-row', 0, 2, [71]))
    clash_order = file_errata(db, SHIFT, 'err-clash', root_number, 'east', clash_piece.event_id, 0, 1)
    clean_order = file_errata(db, SHIFT, 'err-clean', root_number, 'bend', clean_piece.event_id, 0, 1)
    try:
        reissue(
            db, SHIFT, 'night-review-bad-batch', root_number, (clash_order.number, clean_order.number)
        )
    except ReissueConflict as exc:
        conflict_actual = [
            {'line_id': item.line_id, 'span': [item.start, item.stop],
             'current_origin': item.current_origin.event_id,
             'incoming_origin': item.incoming_origin.event_id}
            for item in exc.conflicts
        ]
    else:
        raise RollRowMismatch('多线批次冲突未被拒绝')
    conflict_expected = [{
        'line_id': 'east', 'span': [0, 1],
        'current_origin': 'rt-east-eq', 'incoming_origin': 'rt-east-clash',
    }]
    if conflict_actual != conflict_expected:
        raise RollRowMismatch(f'多线冲突依据不符：{conflict_actual}')
    emit(
        'v12', {'orders': ['err-clash', 'err-clean'], 'base': root_number},
        conflict_expected, conflict_actual, None, None,
        ['handover/revision.py'],
        'east 0 号点自原班单以来已换成 rt-east-eq；bend 勘误点段不相交本不阻塞，但多测线须同次签出，任一处冲突整批拒绝，当前班单 night-review-r2 及其首执保持不变。',
    )

    # 场景三：跨原换卷边界的点段勘误，一次替换 bend 整行两个点（旧卷 head 与新卷 B 各一）。
    cross_piece = receive_piece(db, directory, retest_piece('rt-bend-cross', 'bend', 'bend-row', 0, 2, [70, 130]))
    cross_order = file_errata(db, SHIFT, 'err-bend-cross', root_number, 'bend', cross_piece.event_id, 0, 2)
    rev_cross = reissue(db, SHIFT, 'night-review-r3', root_number, (cross_order.number,))
    delivery = read(db, rev_cross.number)
    actual = {
        'readings': list(line_view(delivery, 'bend').readings),
        'origin_events': origin_events(delivery, 'bend'),
        'totals': [line_view(delivery, 'bend').totals.measured_count,
                   line_view(delivery, 'bend').totals.total_mm],
    }
    expected = {'readings': [70, 130], 'origin_events': [(0, 2, 'rt-bend-cross')], 'totals': [2, 200]}
    if actual != expected:
        raise RollRowMismatch(f'跨换卷勘误结果不符：{actual}')
    emit(
        'v11', {'event_id': 'rt-bend-cross', 'span': [0, 2], 'base': root_number},
        expected, actual, [4, 213],
        [delivery.manifest.totals.measured_count, delivery.manifest.totals.total_mm],
        ['archive/errata.py', 'handover/revision.py'],
        '复测件按点位覆盖旧卷 bend-head 与新卷 bend-B 的交界处，整段换成一处复测出处；手算 bend 200、east 13，全班 4 点 213 mm。',
    )
    print(f'改签三 {rev_cross.number}（版本 {rev_cross.revision}，父版 {rev_cross.parent_number}）')

    # 审核整单拒绝：未实收证据、错线、点段未完整覆盖均不留勘误单。
    audit_piece = receive_piece(db, directory, retest_piece('rt-audit', 'east', 'east-row', 1, 3, [None]))
    audit_cases = [
        ('err-ghost', root_number, 'east', 'no-such-receipt', 0, 1, 'unreceived_evidence'),
        ('err-wrong-line', root_number, 'bend', audit_piece.event_id, 1, 2, 'wrong_line'),
        ('err-uncovered', root_number, 'east', audit_piece.event_id, 0, 2, 'span_not_covered'),
    ]
    for request_id, base, line_id, event_id, start, stop, status in audit_cases:
        try:
            file_errata(db, SHIFT, request_id, base, line_id, event_id, start, stop)
        except ErrataRejected as exc:
            if exc.status != status:
                raise RollRowMismatch(f'{request_id} 拒绝状态不符：{exc.status}')
        else:
            raise RollRowMismatch(f'{request_id} 应当整单拒绝')
    emit(
        'v12b', {'cases': [item[0] for item in audit_cases]},
        ['unreceived_evidence', 'wrong_line', 'span_not_covered'],
        ['unreceived_evidence', 'wrong_line', 'span_not_covered'], None, None,
        ['archive/errata.py'],
        '未完整覆盖申报点段、引用错线或引用未实收证据时整单拒绝，errata_orders 中不留记录。',
    )

    # 同勘误号同参可领原结果，异参拒绝。
    replayed = file_errata(db, SHIFT, 'err-eq', root_number, 'east', equal_piece.event_id, 0, 1)
    if replayed != equal_order:
        raise RollRowMismatch('同号同参未领回原勘误结果')
    try:
        file_errata(db, SHIFT, 'err-eq', root_number, 'east', equal_piece.event_id, 0, 2)
    except ValueError:
        pass
    else:
        raise RollRowMismatch('同号异参未被拒绝')
    return rev_equal, rev_disjoint, rev_cross


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
            # 班单终结、源设备停源后，次班仍可采录复测件并办理勘误改签。
            revisions = run_errata(db, directory, handover.number)
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

        # 停源重开：按签发号分别领原班单和每版改签，核回每段出处。
        for revised in (revisions[0], revisions[1], revisions[2]):
            stored = get_reissue(reopened, revised.number)
            if stored != revised:
                raise RollRowMismatch(f'{revised.number} 重开后领取结果不一致')
            revised_delivery = read(reopened, revised.number)
            revised_receipt = accept(reopened, revised.number)
            print(
                f'改签 {revised.number} 接收首执 {revised_receipt.receipt_id}，'
                f'逐线出处 ' + '；'.join(
                    f'{line.line_id}={[(p.start, p.stop, p.event_id) for p in line.provenance]}'
                    for line in revised_delivery.lines
                )
            )
        root_delivery = read(reopened, handover.number)
        if origin_events(root_delivery, 'bend') != [(0, 1, 'bend-head'), (1, 2, 'bend-B')]:
            raise RollRowMismatch('原班单出处被改签污染')
        emit(
            'v13', {'numbers': [handover.number, *(item.number for item in revisions)]},
            '原班单与三版改签各自保留读值和逐段出处',
            '原班单与三版改签各自保留读值和逐段出处', None, None,
            ['handover/revision.py', 'review/lookup.py', 'handover/accept.py'],
            '停源重开后按签发号离线领取，版本链 root/parent 不变，每段出处均可核回原实收或复测实收。',
        )


def main():
    parser = argparse.ArgumentParser(description='演示班次录制、固定交班、测线勘误和班单改签')
    parser.add_argument('--rollover', action='store_true', help='只核对指定续段的原行及出处')
    options = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='bridge-cape-') as directory:
            run(Path(directory), options.rollover)
    except RollRowMismatch as exc:
        parser.exit(1, f'ROLL_ROW_MISMATCH {exc}\n')


if __name__ == '__main__':
    main()
