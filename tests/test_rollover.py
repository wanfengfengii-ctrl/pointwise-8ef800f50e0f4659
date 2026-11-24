import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from archive.eligibility import check_eligibility
from archive.publication import PublicationBlocked, publication_number, publish
from archive.staging import list_staged_fragments, stage_fragment
from device.recording import RecordingDevice
from intake.capture import capture
from intake.custody import get_capture_receipt, load_received_seal, load_receipted_fragment, store_fragment
from intake.fragments import validate_fragment
from intake.resume import resume
from recording_sample import RECORDING, ROSTER, SHIFT, fragment, ordinary, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, open_shift, station_write


class RolloverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.station('station.sqlite')
        self.bend_number = publication_number(SHIFT, 'bend')

    def station(self, filename):
        db = connect_station(self.root / filename)
        self.addCleanup(db.close)
        open_shift(db, SHIFT, ROSTER)
        return db

    def device(self, events, filename='device.sqlite', **options):
        device = RecordingDevice(self.root / filename, RECORDING, events, **options)
        self.addCleanup(device.close)
        return device

    def receive_fragments(self, device, count, db=None):
        db = self.db if db is None else db
        for _ in range(count):
            result = capture(db, device, SHIFT, device.next_event())
            self.assertEqual(result.status, 'exited')

    def assert_blocked(self, db, status):
        with self.assertRaises(PublicationBlocked) as blocked:
            publish(db, SHIFT, 'bend')
        self.assertEqual(blocked.exception.status, status)
        with self.assertRaises(LookupError):
            read(db, self.bend_number)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publications WHERE number = ?', (self.bend_number,)).fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM publication_receipts WHERE number = ?', (self.bend_number,)).fetchone()[0], 0)

    def test_seal_selects_b(self):
        plan = rollover_events()
        device = self.device(plan)
        self.receive_fragments(device, 3)
        self.assert_blocked(self.db, 'awaiting_seal')
        for attempt in plan[:3]:
            with closing(connect_station(self.root / 'station.sqlite')) as reopened:
                custody = load_receipted_fragment(reopened, SHIFT, attempt.event_id)
                self.assertEqual(custody.attempt, attempt)
            self.assertIsNone(device.read_attempt(attempt.key))
        stated = device.next_event()
        self.assertEqual(stated, plan[-1])
        self.assertEqual(capture(self.db, device, SHIFT, stated).seal, stated)
        signed = publish(self.db, SHIFT, 'bend')
        result = read(self.db, signed.receipt.number)
        self.assertEqual(result.readings, (7, 13))
        self.assertEqual((result.totals.measured_count, result.totals.total_mm), (2, 20))
        self.assertEqual(result.seal.selected, plan[2].key)
        self.assertEqual(
            [(item.start, item.stop, item.roll, item.slot, item.load, item.event_id) for item in result.provenance],
            [(0, 1, 'old-roll', 0, 2, 'bend-head'), (1, 2, 'new-roll-B', 1, 2, 'bend-B')],
        )
        self.assertEqual(result.receipt, signed.receipt)

    def test_selection_is_independent_of_arrival_and_equal_readings(self):
        cases = [
            ('select-a', 'A', ('A', 'B'), 13, (7, 11), ('new-roll-A', 1, 'bend-A')),
            ('reverse', 'B', ('B', 'A'), 13, (7, 13), ('new-roll-B', 1, 'bend-B')),
            ('equal', 'B', ('A', 'B'), 11, (7, 11), ('new-roll-B', 2, 'bend-B')),
        ]
        for label, winner, order, b_value, expected, expected_source in cases:
            with self.subTest(label=label):
                db = self.station(f'{label}-station.sqlite')
                plan = rollover_events(selected=winner, order=order, b_value=b_value)
                device = self.device(plan, f'{label}-device.sqlite')
                self.receive_fragments(device, 3, db)
                candidates = tuple(item.key for item in list_staged_fragments(db, SHIFT, 'bend') if item.kind == 'candidate')
                self.assertEqual(candidates, tuple(event.key for event in plan[1:3]))
                self.assert_blocked(db, 'awaiting_seal')
                capture(db, device, SHIFT, device.next_event())
                receipt = publish(db, SHIFT, 'bend').receipt
                result = read(db, receipt.number)
                self.assertEqual(result.readings, expected)
                source = result.provenance[1]
                self.assertEqual((source.roll, source.load, source.event_id), expected_source)

    def test_gap_and_overlap_are_rejected(self):
        for name, width, start in [('gap', 3, 2), ('overlap', 2, 0)]:
            with self.subTest(name=name):
                db = self.station(f'{name}-station.sqlite')
                plan = rollover_events(total=width, candidate_start=start)
                device = self.device(plan, f'{name}-device.sqlite')
                self.receive_fragments(device, 3, db)
                self.assertEqual(capture(db, device, SHIFT, device.next_event()).status, 'seal_received')
                self.assert_blocked(db, 'invalid_span')

    def test_lost_exit_reply_recovers_original_attempt_after_reopen(self):
        plan = rollover_events()
        original_b = plan[2]
        device = self.device(plan, lost_unload_replies=('bend-B',))
        self.receive_fragments(device, 2)
        pending = capture(self.db, device, SHIFT, device.next_event())
        self.assertEqual((pending.status, pending.receipt.key), ('pending', original_b.key))
        self.assertIsNone(device.read_attempt(original_b.key))
        self.assertEqual(device.query_unload(original_b.key).status, 'exited')
        self.assertEqual(device.query_unload(plan[1].key).event_id, 'bend-A')
        device.close()
        self.db.close()
        self.db = self.station('station.sqlite')
        reopened = self.device(plan, query_available=False)
        self.assertEqual(reopened.position, 3)
        self.assertEqual(load_receipted_fragment(self.db, SHIFT, 'bend-B').attempt, original_b)
        self.assertEqual(resume(self.db, SHIFT, 'bend-B', reopened).status, 'pending')
        reopened.query_available = True
        recovered = resume(self.db, SHIFT, 'bend-B', reopened)
        self.assertEqual((recovered.status, recovered.receipt.key), ('exited', original_b.key))
        self.assertEqual(reopened.unload(original_b.key), reopened.query_unload(original_b.key))
        with closing(sqlite3.connect(self.root / 'device.sqlite')) as source_db:
            self.assertEqual(source_db.execute('SELECT COUNT(*) FROM unload_log WHERE event_id = ?', ('bend-B',)).fetchone()[0], 1)
        capture(self.db, reopened, SHIFT, reopened.next_event())
        self.assertEqual(publish(self.db, SHIFT, 'bend').row.readings, (7, 13))

    def test_conflicting_fragment_and_seal_replays_are_rejected(self):
        plan = rollover_events()
        device = self.device(plan)
        self.receive_fragments(device, 3)
        b = plan[2]
        altered = fragment(b.key, b.event_id, 'bend', 'bend-row', 'candidate', 1, 2, [99])
        with self.assertRaisesRegex(ValueError, 'different content'):
            capture(self.db, device, SHIFT, altered)
        alias = fragment(b.key, 'alias', 'bend', 'bend-row', 'candidate', 1, 2, [13])
        with self.assertRaisesRegex(ValueError, 'another receipt'):
            capture(self.db, device, SHIFT, alias)
        self.assertEqual(load_receipted_fragment(self.db, SHIFT, b.event_id).attempt, b)
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, 'alias'))
        seal = device.next_event()
        first = capture(self.db, device, SHIFT, seal)
        self.assertEqual(capture(self.db, None, SHIFT, seal), first)
        with self.assertRaisesRegex(ValueError, 'different content'):
            capture(self.db, None, SHIFT, replace(seal, selected=plan[1].key))
        self.assertEqual(load_received_seal(self.db, SHIFT, seal.event_id), seal)
        self.assertEqual(publish(self.db, SHIFT, 'bend').row.readings, (7, 13))

    def test_no_winner_does_not_block_independent_line(self):
        plan = (ordinary(), *rollover_events(selected=None))
        device = self.device(plan)
        self.receive_fragments(device, 4)
        capture(self.db, device, SHIFT, device.next_event())
        self.assert_blocked(self.db, 'no_winner')
        ordinary_result = publish(self.db, SHIFT, 'east')
        self.assertEqual(read(self.db, ordinary_result.receipt.number).readings, (4, None, 0))

    def test_archive_waits_for_exact_selected_custody(self):
        plan = rollover_events()
        device = self.device(plan)
        self.receive_fragments(device, 3)
        received = capture(self.db, device, SHIFT, device.next_event()).seal
        archive_input = self.station('independent-input.sqlite')
        with station_write(archive_input):
            for event in (plan[1], plan[0]):
                receipt = store_fragment(archive_input, validate_fragment(event))
                stage_fragment(archive_input, SHIFT, receipt.event_id)
        self.assert_blocked(archive_input, 'awaiting_seal')
        capture(archive_input, None, SHIFT, received)
        self.assert_blocked(archive_input, 'missing_selected')
        self.assertEqual(check_eligibility(archive_input, SHIFT, 'bend').seal, received)
        with station_write(archive_input):
            receipt = store_fragment(archive_input, validate_fragment(plan[2]))
            stage_fragment(archive_input, SHIFT, receipt.event_id)
        signed = publish(archive_input, SHIFT, 'bend')
        self.assertEqual(signed.row.readings, (7, 13))
        self.assertEqual(signed.row.origins[1].key, plan[2].key)

    def test_failed_publication_rolls_back_and_retries_offline(self):
        plan = rollover_events()
        device = self.device(plan)
        self.receive_fragments(device, 3)
        sealed = capture(self.db, device, SHIFT, device.next_event()).seal
        self.db.execute('''CREATE TEMP TRIGGER stop_first_receipt BEFORE INSERT ON publication_receipts
                           WHEN EXISTS (SELECT 1 FROM publications WHERE number = NEW.number)
                           BEGIN SELECT RAISE(ABORT, 'receipt insertion interrupted'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt insertion interrupted'):
            publish(self.db, SHIFT, 'bend')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publications').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_receipts').fetchone()[0], 0)
        with self.assertRaises(LookupError):
            read(self.db, self.bend_number)
        device.close()
        self.db.close()
        self.db = self.station('station.sqlite')
        self.assertEqual(load_received_seal(self.db, SHIFT, sealed.event_id), sealed)
        self.assertEqual(resume(self.db, SHIFT, sealed.event_id).seal, sealed)
        self.assertEqual(capture(self.db, None, SHIFT, sealed).seal, sealed)
        first = publish(self.db, SHIFT, 'bend')
        self.assertEqual(first.row.readings, (7, 13))
        self.assertEqual(first.row.origins[1].key, plan[2].key)
        self.assertEqual(publish(self.db, SHIFT, 'bend'), first)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_receipts').fetchone()[0], 1)
        self.db.close()
        db = self.station('station.sqlite')
        self.assertEqual(read(db, self.bend_number).receipt, first.receipt)
        self.assertEqual(read(db, self.bend_number).provenance[1].event_id, 'bend-B')
