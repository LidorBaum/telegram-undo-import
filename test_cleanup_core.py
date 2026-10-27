"""Unit tests for the selection and bookkeeping logic. Run: python -m unittest -v"""
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cleanup_core as core

ME, HIM, OTHER = 111, 222, 333
TZ = ZoneInfo("Asia/Jerusalem")


def make_config(**overrides) -> core.Config:
    base = core.Config(
        self_user_id=ME, target_user_id=HIM, date=date(2026, 10, 27), timezone="Asia/Jerusalem",
        batch_size=100, delay_seconds=2.0, flood_wait_margin_seconds=5, max_retries=5, session_name="s",
    )
    return replace(base, **overrides)


def msg(local_dt: datetime, *, out=True, peer=HIM, sender=None, imported=True, id=1) -> core.MessageFacts:
    """`local_dt` is when the message appeared in the chat (the import time, for imported ones)."""
    return core.MessageFacts(id=id, date=local_dt.replace(tzinfo=TZ).astimezone(timezone.utc), out=out,
                             peer_id=peer, from_id=sender, kind="text", imported=imported)


class WindowTests(unittest.TestCase):
    def test_target_day_is_utc_plus_2_after_dst_ended(self):
        start, end = core.day_window_utc(date(2026, 10, 27), "Asia/Jerusalem")
        self.assertEqual(start, datetime(2026, 10, 26, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 27, 22, 0, tzinfo=timezone.utc))

    def test_dst_start_day_is_23_hours(self):
        start, end = core.day_window_utc(date(2026, 3, 27), "Asia/Jerusalem")
        self.assertEqual(end - start, timedelta(hours=23))

    def test_boundaries_start_inclusive_end_exclusive(self):
        start, end = core.day_window_utc(date(2026, 10, 27), "Asia/Jerusalem")
        self.assertTrue(core.in_window(start, start, end))
        self.assertFalse(core.in_window(end, start, end))
        self.assertFalse(core.in_window(start - timedelta(microseconds=1), start, end))

    def test_naive_datetime_rejected(self):
        start, end = core.day_window_utc(date(2026, 10, 27), "Asia/Jerusalem")
        with self.assertRaises(ValueError):
            core.in_window(datetime(2026, 10, 27, 12), start, end)


class SelectionRuleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.start, self.end = core.day_window_utc(self.cfg.date, self.cfg.timezone)

    def check(self, m):
        return core.is_deletable(m, self.cfg, self.start, self.end)

    def test_imported_that_day_is_selected_from_both_sides(self):
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 15, 7), out=True)))
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 15, 7), out=True, sender=ME)))
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 15, 7), out=False)))
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 15, 7), out=False, sender=HIM)))

    def test_appeared_today_boundaries(self):
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 0, 0, 0))))
        self.assertTrue(self.check(msg(datetime(2026, 10, 27, 23, 59, 59))))
        self.assertFalse(self.check(msg(datetime(2026, 10, 26, 23, 59, 59))))  # imported on an earlier day
        self.assertFalse(self.check(msg(datetime(2026, 10, 28, 0, 0, 0))))

    def test_utc_date_alone_does_not_decide(self):
        # 23:30 UTC on Oct 26 is 01:30 on Oct 27 in Jerusalem -> selected.
        m = msg(datetime(2026, 10, 27, 12))
        self.assertTrue(self.check(replace(m, date=datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc))))
        # 22:30 UTC on Oct 27 is 00:30 on Oct 28 in Jerusalem -> not selected.
        self.assertFalse(self.check(replace(m, date=datetime(2026, 10, 27, 22, 30, tzinfo=timezone.utc))))

    def test_real_telegram_messages_are_never_selected(self):
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 15, 7), imported=False, out=True)))
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 15, 7), imported=False, out=False, sender=HIM)))

    def test_other_chats_are_never_selected(self):
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 12), peer=OTHER)))
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 12), peer=None)))
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 12), peer=-1001234)))

    def test_foreign_sender_is_never_selected(self):
        self.assertFalse(self.check(msg(datetime(2026, 10, 27, 12), sender=OTHER)))


class ConfigTests(unittest.TestCase):
    def write(self, **overrides):
        raw = json.loads(Path(__file__).with_name("config.example.json").read_text())
        raw.update({"self_user_id": ME, "target_user_id": HIM, **overrides})
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(raw, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_example_config_loads(self):
        cfg = core.load_config(self.write())
        self.assertEqual(cfg.date, date(2026, 10, 27))
        cfg.require_ids()

    def test_rejects_unsafe_values(self):
        for bad in ({"batch_size": 101}, {"batch_size": 0}, {"delay_seconds": 0}, {"timezone": "Mars/Base"},
                    {"date": "27/10/2026"}, {"session_name": "../x"}, {"self_user_id": "111"},
                    {"max_retries": -1}):
            with self.subTest(bad=bad), self.assertRaises(core.ConfigError):
                core.load_config(self.write(**bad))

    def test_ids_required_and_distinct(self):
        with self.assertRaises(core.ConfigError):
            make_config(target_user_id=0).require_ids()
        with self.assertRaises(core.ConfigError):
            make_config(target_user_id=ME).require_ids()


class BookkeepingTests(unittest.TestCase):
    def test_chunks_never_exceed_100(self):
        batches = core.chunks(list(range(11_050)), 100)
        self.assertEqual(len(batches), 111)
        self.assertTrue(all(len(b) <= 100 for b in batches))
        self.assertEqual(sum(batches, []), list(range(11_050)))
        with self.assertRaises(ValueError):
            core.chunks([1], 101)

    def test_digest_ignores_order(self):
        self.assertEqual(core.ids_digest([3, 1, 2]), core.ids_digest([1, 2, 3]))
        self.assertNotEqual(core.ids_digest([1, 2]), core.ids_digest([1, 2, 3]))

    def test_manifest_problems(self):
        cfg = make_config()
        manifest = {"self_user_id": ME, "target_user_id": HIM, "date": "2026-10-27", "timezone": "Asia/Jerusalem",
                    "selection_rule": core.SELECTION_RULE, "matches": [{"id": 5}, {"id": 6}],
                    "match_count": 2, "match_ids_sha256": core.ids_digest([5, 6])}
        self.assertEqual(core.manifest_problems(manifest, cfg), [])
        self.assertTrue(core.manifest_problems({**manifest, "target_user_id": OTHER}, cfg))
        self.assertTrue(core.manifest_problems({**manifest, "selection_rule": None}, cfg))  # old-rule manifest
        self.assertTrue(core.manifest_problems({**manifest, "matches": [{"id": 5}, {"id": 7}]}, cfg))

    def test_progress_roundtrip_and_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "progress.json"
            p = core.Progress(manifest_sha256="abc", deleted=[1, 2], failed={"2": "x", "3": "y"})
            core.save_progress(path, p)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = core.load_progress(path, "abc")
            self.assertEqual(loaded.deleted, [1, 2])
            self.assertEqual(loaded.failed, {"3": "y"})  # 2 was deleted, so its failure is cleared
            with self.assertRaises(ValueError):
                core.load_progress(path, "different")


if __name__ == "__main__":
    unittest.main()
