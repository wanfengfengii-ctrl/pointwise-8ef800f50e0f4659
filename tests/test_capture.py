import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from archive.publication import PublicationBlocked, publication_number, publish
from archive.staging import list_staged_fragments
from device.buffers import AttemptKey
from device.recording import RecordingDevice
from intake.capture import capture
from intake.custody import get_capture_receipt, load_receipted_fragment
from intake.resume import resume
from recording_sample import RECORDING, ROSTER, SHIFT, fragment, ordinary, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, get_shift, open_shift


class CaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.station = self.root / 'station.sqlite'
        self.db = self.connect()
        open_shift(self.db, SHIFT, ROSTER)

    def connect(self):
        db = connect_station(self.station)
        self.addCleanup(db.close)
        return db

    def device(self, events, **options):
        device = RecordingDevice(self.root / 'device.sqlite', RECORDING, events, **options)
        self.addCleanup(device.close)
        return device

    def test_ordinary_custody_precedes_exit_and_zero_counts(self):
        original = ordinary()
        device = self.device((original,), query_available=False)
        event = device.next_event()
        result = capture(self.db, device, SHIFT, event)
        self.assertEqual(result.status, 'pending')
        self.assertEqual(device.read_attempt(original.key), original)
        self.db.close()
        self.db = self.connect()
        staged, = list_staged_fragments(self.db, SHIFT, 'east')
        self.assertEqual(staged.key, original.key)
        self.assertEqual(load_receipted_fragment(self.db, SHIFT, original.event_id).attempt, original)
        device.query_available = True
        self.assertEqual(resume(self.db, SHIFT, original.event_id, device).status, 'exited')
        self.assertIsNone(device.read_attempt(original.key))
        self.assertIsNone(device.replay_event(original.event_id))
        signed = publish(self.db, SHIFT, 'east')
        view = read(self.db, signed.receipt.number)
        self.assertEqual(view.readings, (4, None, 0))
        self.assertEqual((view.totals.measured_count, view.totals.total_mm), (2, 4))
        source, = view.provenance
        self.assertEqual((source.start, source.stop, source.roll, source.slot, source.load), (0, 3, 'lead-roll', 0, 1))
        self.assertEqual((source.event_id, source.checksum), (original.event_id, original.checksum))
        self.assertEqual(capture(self.db, None, SHIFT, original).status, 'exited')
        self.assertEqual(publish(self.db, SHIFT, 'east'), signed)

    def test_reopening_reuses_roster_and_rejects_changed_configuration(self):
        config = get_shift(self.db, SHIFT)
        self.db.close()
        self.db = self.connect()
        self.assertEqual(open_shift(self.db, SHIFT, reversed(ROSTER)), config)
        for lines, unit in [(('other',), 'mm'), (('east', 'east'), 'mm'), (ROSTER, 'cm'), ((), 'mm')]:
            with self.subTest(lines=lines, unit=unit), self.assertRaises(ValueError):
                open_shift(self.db, SHIFT, lines, unit)
        self.assertEqual(get_shift(self.db, SHIFT), config)
        device = self.device((ordinary(),))
        self.assertEqual(capture(self.db, device, SHIFT, device.next_event()).status, 'exited')
        self.assertEqual(publish(self.db, SHIFT, 'east').row.readings, (4, None, 0))

    def test_bad_checksum_keeps_source_and_leaves_no_custody(self):
        original = ordinary()
        bad = replace(original, checksum='0' * 64)
        device = self.device((bad,))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            capture(self.db, device, SHIFT, device.next_event())
        self.assertEqual(device.replay_event(bad.event_id), bad)
        self.assertEqual(device.query_unload(bad.key).status, 'resident')
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, bad.event_id))
        self.assertEqual(list_staged_fragments(self.db, SHIFT, 'east'), ())
        with self.assertRaises(PublicationBlocked):
            publish(self.db, SHIFT, 'east')
        with self.assertRaises(LookupError):
            read(self.db, publication_number(SHIFT, 'east'))
        with self.assertRaisesRegex(ValueError, 'different events'):
            self.device((original,))

    def test_staging_write_failure_rolls_back_custody_and_keeps_source(self):
        event = ordinary()
        device = self.device((event,))
        self.db.execute('''CREATE TEMP TRIGGER stop_staging BEFORE INSERT ON staged_fragments
                           BEGIN SELECT RAISE(ABORT, 'staging interrupted'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'staging interrupted'):
            capture(self.db, device, SHIFT, device.next_event())
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, event.event_id))
        self.assertEqual(device.read_attempt(event.key), event)
        self.db.execute('DROP TRIGGER stop_staging')
        self.assertEqual(capture(self.db, device, SHIFT, device.replay_event(event.event_id)).status, 'exited')

    def test_false_measurement_and_wrong_line_are_rejected(self):
        bad_value = fragment(AttemptKey(RECORDING, 'r', 0, 1), 'bool', 'east', 'r1', 'whole', 0, 1, [False])
        bad_line = fragment(AttemptKey(RECORDING, 'r', 1, 1), 'foreign', 'outside', 'r2', 'whole', 0, 1, [3])
        device = self.device((bad_value, bad_line))
        for event in (bad_value, bad_line):
            with self.subTest(event=event.event_id), self.assertRaises(ValueError):
                capture(self.db, device, SHIFT, device.next_event())
            self.assertEqual(device.query_unload(event.key).status, 'resident')
            self.assertIsNone(get_capture_receipt(self.db, SHIFT, event.event_id))

    def test_all_missing_and_large_signed_integers_round_trip(self):
        large = 10 ** 90
        empty = fragment(AttemptKey(RECORDING, 'r', 0, 1), 'miss', 'east', 'r1', 'whole', 0, 2, [None, None])
        exact = fragment(AttemptKey(RECORDING, 'r', 1, 1), 'exact', 'spur', 'r2', 'whole', 0, 2, [large, -large + 9])
        device = self.device((empty, exact))
        for event in (empty, exact):
            self.assertEqual(capture(self.db, device, SHIFT, device.next_event()).status, 'exited')
        numbers = [publish(self.db, SHIFT, line).receipt.number for line in ('east', 'spur')]
        self.db.close()
        with_db = self.connect()
        missing, measured = [read(with_db, number) for number in numbers]
        self.assertEqual((missing.readings, missing.totals.measured_count, missing.totals.total_mm), ((None, None), 0, 0))
        self.assertEqual((measured.readings, measured.totals.measured_count, measured.totals.total_mm), ((large, -large + 9), 2, 9))

    def test_two_slots_and_blocked_event_survive_reopen(self):
        events = rollover_events()
        old, a, b, seal = events
        device = self.device(events)
        self.assertEqual(device.next_event(), old)
        self.assertEqual(device.next_event(), a)
        self.assertEqual((device.read_attempt(old.key), device.read_attempt(a.key)), (old, a))
        self.assertIsNone(device.next_event())
        self.assertEqual(device.position, 2)
        device.close()
        reopened = self.device(events)
        self.assertEqual(reopened.replay_event(a.event_id), a)
        capture(self.db, reopened, SHIFT, a)
        self.assertEqual(reopened.next_event(), b)
        self.assertIsNone(reopened.next_event())
        self.assertEqual(reopened.position, 3)
        capture(self.db, reopened, SHIFT, old)
        capture(self.db, reopened, SHIFT, b)
        self.assertEqual(reopened.next_event(), seal)
        self.assertEqual(reopened.replay_event(seal.event_id), seal)
        capture(self.db, reopened, SHIFT, seal)
        self.assertEqual(publish(self.db, SHIFT, 'bend').row.readings, (7, 13))
