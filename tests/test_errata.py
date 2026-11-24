import tempfile
import unittest
from pathlib import Path

from archive.errata import (
    ErrataRejected, effective_line_state, errata_number, file_errata, get_errata,
)
from archive.publication import publish
from device.buffers import AttemptKey
from device.recording import RecordingDevice
from handover.accept import accept
from handover.issue import issue
from handover.revision import ReissueConflict, get_reissue, reissue
from intake.capture import capture
from intake.retest import receive_retest
from recording_sample import RECORDING, ROSTER, SHIFT, ordinary, retest, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, open_shift

RETEST_REC = 'bridge-cape-retest'


class ErrataReissueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.station()
        device = self.device('device.sqlite', RECORDING, (ordinary(), *rollover_events()))
        capture(self.db, device, SHIFT, device.next_event())
        for _ in range(4):
            capture(self.db, device, SHIFT, device.next_event())
        self.east = publish(self.db, SHIFT, 'east')
        self.bend = publish(self.db, SHIFT, 'bend')
        self.h0 = issue(self.db, SHIFT, 'root', ('bend', 'east'))
        self.accept0 = accept(self.db, self.h0.number)
        device.close()

    def station(self, name='station.sqlite'):
        db = connect_station(self.root / name)
        self.addCleanup(db.close)
        open_shift(db, SHIFT, ROSTER)
        return db

    def device(self, name, recording_id, events, **options):
        device = RecordingDevice(self.root / name, recording_id, events, **options)
        self.addCleanup(device.close)
        return device

    def retest_piece(self, label, event_id, line_id, row_id, start, total, values, roll=None):
        roll = roll or f'roll-{event_id}'
        piece = retest(AttemptKey(RETEST_REC, roll, 0, 1), event_id, line_id, row_id, start, total, values)
        device = self.device(f'{label}.sqlite', RETEST_REC, (piece,))
        result = receive_retest(self.db, device, SHIFT, device.next_event())
        device.close()
        self.assertEqual(result.receipt.exit_status, 'exited')
        return piece

    def errata(self, request_id, base, line_id, event_id, start, stop):
        return file_errata(self.db, SHIFT, request_id, base, line_id, event_id, start, stop)

    def accept(self, number):
        return accept(self.db, number)

    def reopen(self):
        self.db.close()
        self.db = self.station()
        return self.db

    # ---- 复测件 ----------------------------------------------------------
    def test_retest_piece_is_receipted_but_never_published(self):
        piece = self.retest_piece('p1', 'rt-east-0', 'east', 'east-row', 0, 3, [4])
        from shift.open_shift import station_write
        with self.assertRaisesRegex(ValueError, 'retest'):
            from archive.staging import stage_fragment
            with station_write(self.db):
                stage_fragment(self.db, SHIFT, piece.event_id)
        self.assertEqual(
            read(self.db, self.east.receipt.number).provenance[0].event_id, 'east-whole'
        )

    def test_retest_can_be_received_after_shift_closure(self):
        from handover.issue import finish_shift_handover
        finish_shift_handover(self.db, SHIFT, self.h0.number)
        piece = self.retest_piece('p-closed', 'rt-after', 'east', 'east-row', 2, 3, [8])
        order = self.errata('after-closure', self.h0.number, 'east', piece.event_id, 2, 3)
        self.assertEqual(order.state.readings, (4, None, 8))

    # ---- 勘误审核 --------------------------------------------------------
    def test_unreceived_evidence_wrong_line_and_uncovered_span_reject_whole_order(self):
        with self.assertRaises(ErrataRejected) as blocked:
            file_errata(self.db, SHIFT, 'ghost', self.h0.number, 'east', 'no-such-event', 0, 1)
        self.assertEqual(blocked.exception.status, 'unreceived_evidence')
        with self.assertRaises(LookupError):
            get_errata(self.db, errata_number(SHIFT, 'ghost'))
        piece = self.retest_piece('p2', 'rt-east-1', 'east', 'east-row', 1, 3, [None])
        with self.assertRaises(ErrataRejected) as blocked:
            file_errata(self.db, SHIFT, 'wide', self.h0.number, 'east', piece.event_id, 0, 2)
        self.assertEqual(blocked.exception.status, 'span_not_covered')
        with self.assertRaises(ErrataRejected) as blocked:
            file_errata(self.db, SHIFT, 'wrong-line', self.h0.number, 'bend', piece.event_id, 1, 2)
        self.assertEqual(blocked.exception.status, 'wrong_line')
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM errata_orders').fetchone()[0], 0
        )

    def test_errata_replaces_only_declared_span_and_keeps_original_provenance(self):
        piece = self.retest_piece('p3', 'rt-east-2', 'east', 'east-row', 2, 3, [9])
        order = self.errata('span-only', self.h0.number, 'east', piece.event_id, 2, 3)
        self.assertEqual(order.state.readings, (4, None, 9))
        origins = [(item.start, item.stop, item.event_id) for item in order.state.provenance]
        self.assertEqual(origins, [(0, 2, 'east-whole'), (2, 3, 'rt-east-2')])
        self.assertEqual(order.receipt_id, f'errata-receipt/{order.number}')

    def test_same_errata_number_same_params_replays_other_params_rejected(self):
        piece = self.retest_piece('p4', 'rt-idem', 'east', 'east-row', 2, 3, [9])
        first = self.errata('idem', self.h0.number, 'east', piece.event_id, 2, 3)
        again = self.errata('idem', self.h0.number, 'east', piece.event_id, 2, 3)
        self.assertEqual(again, first)
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.errata('idem', self.h0.number, 'east', piece.event_id, 1, 3)

    def test_one_retest_piece_cannot_back_two_errata_orders(self):
        piece = self.retest_piece('p4b', 'rt-shared', 'east', 'east-row', 2, 3, [9])
        first = self.errata('shared-a', self.h0.number, 'east', piece.event_id, 2, 3)
        with self.assertRaisesRegex(ValueError, 'already backs'):
            self.errata('shared-b', self.h0.number, 'east', piece.event_id, 2, 3)
        self.assertEqual(get_errata(self.db, first.number), first)
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM errata_orders').fetchone()[0], 1
        )

    def test_signed_errata_cannot_be_reissued_again_but_same_request_replays(self):
        piece = self.retest_piece('p4c', 'rt-once', 'east', 'east-row', 2, 3, [9])
        order = self.errata('once', self.h0.number, 'east', piece.event_id, 2, 3)
        revised = reissue(self.db, SHIFT, 'once-rev', self.h0.number, (order.number,))
        self.assertEqual(
            reissue(self.db, SHIFT, 'once-rev', self.h0.number, (order.number,)), revised
        )
        with self.assertRaisesRegex(ValueError, 'already signed into'):
            reissue(self.db, SHIFT, 'once-rev-again', self.h0.number, (order.number,))

    # ---- 等值异出处 ------------------------------------------------------
    def test_equal_value_but_new_origin_is_signed_and_conflicts_when_target_taken(self):
        first_piece = self.retest_piece('p5a', 'rt-e0-a', 'east', 'east-row', 0, 3, [4])
        first_order = self.errata('eq-a', self.h0.number, 'east', first_piece.event_id, 0, 1)
        revised = reissue(self.db, SHIFT, 'eq-rev-1', self.h0.number, (first_order.number,))
        delivery = read(self.db, revised.number)
        east = next(line for line in delivery.lines if line.line_id == 'east')
        self.assertEqual(east.readings, (4, None, 0))
        self.assertEqual(east.provenance[0].event_id, 'rt-e0-a')
        second_piece = self.retest_piece('p5b', 'rt-e0-b', 'east', 'east-row', 0, 3, [4])
        second_order = self.errata('eq-b', self.h0.number, 'east', second_piece.event_id, 0, 1)
        with self.assertRaises(ReissueConflict) as conflict:
            reissue(self.db, SHIFT, 'eq-rev-bad', self.h0.number, (second_order.number,))
        detail = conflict.exception.conflicts[0]
        self.assertEqual((detail.line_id, detail.start, detail.stop), ('east', 0, 1))
        self.assertEqual(detail.current_origin.event_id, 'rt-e0-a')
        self.assertEqual(detail.incoming_origin.event_id, 'rt-e0-b')
        self.assertEqual(self.accept(self.h0.number), self.accept0)
        with self.assertRaises(LookupError):
            get_reissue(self.db, 'handover/eq-rev-bad')

    # ---- 不相交点段与版本链 ----------------------------------------------
    def test_two_independent_retests_from_same_old_version_use_disjoint_spans(self):
        piece_zero = self.retest_piece('p6a', 'rt-z', 'east', 'east-row', 0, 3, [5])
        piece_two = self.retest_piece('p6b', 'rt-t', 'east', 'east-row', 2, 3, [9])
        order_zero = self.errata('d1', self.h0.number, 'east', piece_zero.event_id, 0, 1)
        order_two = self.errata('d2', self.h0.number, 'east', piece_two.event_id, 2, 3)
        rev_one = reissue(self.db, SHIFT, 'disjoint-1', self.h0.number, (order_zero.number,))
        rev_two = reissue(self.db, SHIFT, 'disjoint-2', self.h0.number, (order_two.number,))
        self.assertEqual(rev_one.parent_number, self.h0.number)
        self.assertEqual(rev_two.parent_number, rev_one.number)
        delivery = read(self.db, rev_two.number)
        east = next(line for line in delivery.lines if line.line_id == 'east')
        self.assertEqual(east.readings, (5, None, 9))
        self.assertEqual([item.event_id for item in east.provenance], ['rt-z', 'east-whole', 'rt-t'])
        self.assertEqual((delivery.manifest.totals.measured_count, delivery.manifest.totals.total_mm), (4, 34))

    def test_adjacent_disjoint_spans_in_one_signing_do_not_block_each_other(self):
        piece_a = self.retest_piece('p7a', 'rt-adj-a', 'east', 'east-row', 0, 3, [6])
        piece_b = self.retest_piece('p7b', 'rt-adj-b', 'east', 'east-row', 1, 3, [None])
        order_a = self.errata('adj-a', self.h0.number, 'east', piece_a.event_id, 0, 1)
        order_b = self.errata('adj-b', self.h0.number, 'east', piece_b.event_id, 1, 2)
        revised = reissue(
            self.db, SHIFT, 'adj-rev', self.h0.number, (order_a.number, order_b.number)
        )
        east = next(
            line for line in read(self.db, revised.number).lines if line.line_id == 'east'
        )
        self.assertEqual(east.readings, (6, None, 0))
        self.assertEqual([item.event_id for item in east.provenance], ['rt-adj-a', 'rt-adj-b', 'east-whole'])

    def test_intersecting_spans_in_one_batch_are_rejected_together(self):
        piece_a = self.retest_piece('p8a', 'rt-ov-a', 'east', 'east-row', 0, 3, [6, 7])
        piece_b = self.retest_piece('p8b', 'rt-ov-b', 'east', 'east-row', 1, 3, [8, 0])
        order_a = self.errata('ov-a', self.h0.number, 'east', piece_a.event_id, 0, 2)
        order_b = self.errata('ov-b', self.h0.number, 'east', piece_b.event_id, 1, 2)
        with self.assertRaises(ReissueConflict) as conflict:
            reissue(self.db, SHIFT, 'ov-rev', self.h0.number, (order_a.number, order_b.number))
        self.assertEqual(len(conflict.exception.conflicts), 1)
        with self.assertRaises(LookupError):
            get_reissue(self.db, 'handover/ov-rev')

    # ---- 跨换卷 ----------------------------------------------------------
    def test_correction_crosses_original_rollover_boundary_by_point(self):
        piece = self.retest_piece('p9', 'rt-bend-all', 'bend', 'bend-row', 0, 2, [70, 130])
        order = self.errata('cross', self.h0.number, 'bend', piece.event_id, 0, 2)
        revised = reissue(self.db, SHIFT, 'cross-rev', self.h0.number, (order.number,))
        bend = next(line for line in read(self.db, revised.number).lines if line.line_id == 'bend')
        self.assertEqual(bend.readings, (70, 130))
        self.assertEqual(len(bend.provenance), 1)
        self.assertEqual(bend.provenance[0].event_id, 'rt-bend-all')
        self.assertEqual((bend.totals.measured_count, bend.totals.total_mm), (2, 200))
        original = read(self.db, self.h0.number)
        old_bend = next(line for line in original.lines if line.line_id == 'bend')
        self.assertEqual(old_bend.readings, (7, 13))
        self.assertEqual([item.event_id for item in old_bend.provenance], ['bend-head', 'bend-B'])

    # ---- 多线同次签出与整单回滚 ------------------------------------------
    def test_multi_line_reissue_is_atomic_and_reports_both_bases_on_conflict(self):
        good = self.retest_piece('p10a', 'rt-ml-good', 'bend', 'bend-row', 1, 2, [99])
        taken = self.retest_piece('p10b', 'rt-ml-taken', 'east', 'east-row', 0, 3, [5])
        blocker_piece = self.retest_piece('p10c', 'rt-ml-again', 'east', 'east-row', 0, 3, [8])
        blocked_revision = reissue(
            self.db, SHIFT, 'ml-head', self.h0.number,
            (self.errata('ml-e0', self.h0.number, 'east', taken.event_id, 0, 1).number,),
        )
        good_order = self.errata('ml-bend', self.h0.number, 'bend', good.event_id, 1, 2)
        blocked_order = self.errata('ml-east', self.h0.number, 'east', blocker_piece.event_id, 0, 1)
        with self.assertRaises(ReissueConflict) as conflict:
            reissue(
                self.db, SHIFT, 'ml-bad', self.h0.number, (good_order.number, blocked_order.number)
            )
        detail = conflict.exception.conflicts[0]
        self.assertEqual(detail.line_id, 'east')
        self.assertEqual(detail.current_origin.event_id, 'rt-ml-taken')
        self.assertEqual(detail.incoming_origin.event_id, 'rt-ml-again')
        self.assertEqual(self.accept(self.h0.number), self.accept0)
        self.assertEqual(
            read(self.db, blocked_revision.number).manifest.totals,
            blocked_revision.totals,
        )
        with self.assertRaises(LookupError):
            get_reissue(self.db, 'handover/ml-bad')

    def test_old_version_reference_remains_valid_when_target_span_untouched_since(self):
        piece = self.retest_piece('p11', 'rt-old-ref', 'east', 'east-row', 2, 3, [3])
        head = reissue(
            self.db, SHIFT, 'chain-head', self.h0.number,
            (self.errata('c-east0', self.h0.number, 'east',
                         self.retest_piece('p11b', 'rt-c0', 'east', 'east-row', 0, 3, [2]).event_id,
                         0, 1).number,),
        )
        order = self.errata('old-base', self.h0.number, 'east', piece.event_id, 2, 3)
        revised = reissue(self.db, SHIFT, 'chain-next', self.h0.number, (order.number,))
        self.assertEqual(revised.parent_number, head.number)
        east = next(line for line in read(self.db, revised.number).lines if line.line_id == 'east')
        self.assertEqual(east.readings, (2, None, 3))

    def test_reissue_idempotency_and_chain_lookup_survive_reopen(self):
        piece = self.retest_piece('p12', 'rt-reopen', 'east', 'east-row', 2, 3, [4])
        order = self.errata('rp', self.h0.number, 'east', piece.event_id, 2, 3)
        revised = reissue(self.db, SHIFT, 'rp-rev', self.h0.number, (order.number,))
        same = reissue(self.db, SHIFT, 'rp-rev', self.h0.number, (order.number,))
        self.assertEqual(same, revised)
        other_order = self.errata(
            'rp-other', self.h0.number, 'east',
            self.retest_piece('p12c', 'rt-reopen-other', 'east', 'east-row', 0, 3, [2]).event_id, 0, 1
        )
        with self.assertRaisesRegex(ValueError, 'different request'):
            reissue(self.db, SHIFT, 'rp-rev', self.h0.number, (other_order.number,))
        receipt = accept(self.db, revised.number)
        self.assertEqual(receipt.receipt_id, f'acceptance-receipt/{revised.number}')
        self.assertEqual(accept(self.db, revised.number), receipt)
        self.reopen()
        self.assertEqual(read(self.db, self.h0.number).lines[1].readings, (4, None, 0))
        reopened_delivery = read(self.db, revised.number)
        self.assertEqual(reopened_delivery.lines[1].readings, (4, None, 4))
        self.assertEqual(
            [item.event_id for item in reopened_delivery.lines[1].provenance],
            ['east-whole', 'rt-reopen'],
        )
        self.assertEqual(get_reissue(self.db, revised.number).errata_numbers, (order.number,))
        self.assertEqual(accept(self.db, revised.number), receipt)
        self.assertEqual(effective_line_state(self.db, revised.number, 'east').readings, (4, None, 4))


if __name__ == '__main__':
    unittest.main()
