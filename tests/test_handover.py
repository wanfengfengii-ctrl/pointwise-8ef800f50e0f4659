import tempfile
import unittest
from pathlib import Path

from archive.publication import publish
from device.recording import RecordingDevice
from handover.accept import accept
from handover.issue import finish_shift_handover, get_issue, handover_number, issue
from intake.capture import capture
from intake.resume import resume
from recording_sample import RECORDING, ROSTER, SHIFT, ordinary, rollover_events, spur
from review.lookup import read
from shift.close_shift import get_closure
from shift.open_shift import connect_station, get_shift, open_shift


class HandoverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.connect()
        open_shift(self.db, SHIFT, ROSTER)

    def connect(self):
        db = connect_station(self.root / 'station.sqlite')
        self.addCleanup(db.close)
        return db

    def device(self, events):
        device = RecordingDevice(self.root / 'device.sqlite', RECORDING, events)
        self.addCleanup(device.close)
        return device

    def test_old_manifest_keeps_original_results_after_later_publication(self):
        device = self.device((ordinary(), *rollover_events(), spur()))
        capture(self.db, device, SHIFT, device.next_event())
        east = publish(self.db, SHIFT, 'east')
        old = issue(self.db, SHIFT, 'old', ('east',))
        view = read(self.db, old.number)
        first = accept(self.db, old.number)
        self.assertEqual(first.delivery, view)
        self.assertEqual(view.lines[0].readings, (4, None, 0))
        self.assertEqual(view.lines[0].provenance[0].event_id, 'east-whole')
        self.assertEqual(view.lines[0].receipt, east.receipt)
        self.assertEqual((view.manifest.totals.measured_count, view.manifest.totals.total_mm), (2, 4))
        for _ in range(4):
            capture(self.db, device, SHIFT, device.next_event())
        bend = publish(self.db, SHIFT, 'bend')
        second = issue(self.db, SHIFT, 'combined', ('east', 'bend'))
        self.assertEqual(issue(self.db, SHIFT, 'combined', ('bend', 'east')), second)
        combined = read(self.db, second.number)
        self.assertEqual(tuple(line.receipt for line in combined.lines), (bend.receipt, east.receipt))
        self.assertEqual(tuple(line.readings for line in combined.lines), ((7, 13), (4, None, 0)))
        self.assertEqual((combined.manifest.totals.measured_count, combined.manifest.totals.total_mm), (4, 24))
        self.assertEqual([(entry.totals.measured_count, entry.totals.total_mm) for entry in combined.manifest.entries], [(2, 20), (2, 4)])
        closure = finish_shift_handover(self.db, SHIFT, second.number)
        self.assertEqual(closure.config, get_shift(self.db, SHIFT))
        unreceived = device.next_event()
        with self.assertRaisesRegex(ValueError, 'closed'):
            capture(self.db, device, SHIFT, unreceived)
        self.assertEqual(device.read_attempt(unreceived.key), unreceived)
        device.close()
        self.db.close()
        self.db = self.connect()
        self.assertEqual(get_closure(self.db, SHIFT), closure)
        self.assertEqual(finish_shift_handover(self.db, SHIFT, second.number), closure)
        self.assertEqual(read(self.db, old.number), view)
        self.assertEqual(accept(self.db, old.number), first)
        self.assertEqual(read(self.db, second.number), combined)
        self.assertEqual(resume(self.db, SHIFT, 'bend-seal').seal.event_id, 'bend-seal')
        self.assertEqual(resume(self.db, SHIFT, 'east-whole').status, 'exited')
        with self.assertRaisesRegex(ValueError, 'different request'):
            issue(self.db, SHIFT, 'combined', ('east',))
        with self.assertRaisesRegex(ValueError, 'different handover'):
            finish_shift_handover(self.db, SHIFT, old.number)

    def test_pending_line_rejects_entire_handover_and_preserves_old_issue(self):
        device = self.device((ordinary(), *rollover_events()))
        capture(self.db, device, SHIFT, device.next_event())
        east = publish(self.db, SHIFT, 'east')
        capture(self.db, device, SHIFT, device.next_event())
        old_issue = issue(self.db, SHIFT, 'existing', ('east',))
        old_acceptance = accept(self.db, old_issue.number)
        failed_number = handover_number('mixed')
        with self.assertRaises(LookupError):
            issue(self.db, SHIFT, 'mixed', ('east', 'bend'))
        for table in ('manifests', 'handover_issues', 'handover_receipts'):
            self.assertEqual(self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)
        with self.assertRaises(LookupError):
            read(self.db, failed_number)
        with self.assertRaises(LookupError):
            accept(self.db, failed_number)
        with self.assertRaises(LookupError):
            finish_shift_handover(self.db, SHIFT, failed_number)
        self.assertIsNone(get_closure(self.db, SHIFT))
        self.assertEqual(get_issue(self.db, old_issue.number), old_issue)
        self.assertEqual(accept(self.db, old_issue.number), old_acceptance)
        self.assertEqual(read(self.db, east.receipt.number).readings, (4, None, 0))
        open_shift(self.db, 'other-watch', ('east',))
        with self.assertRaises(LookupError):
            finish_shift_handover(self.db, 'other-watch', old_issue.number)
        with self.assertRaises(LookupError):
            issue(self.db, 'other-watch', 'foreign', ('east',))
