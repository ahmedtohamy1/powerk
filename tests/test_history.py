import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import powerk


MAC = "AABBCCDDEEFF"


def sample(at, energy=5, mac=MAC):
    device = powerk.Device(mac, "MTTL-W01", "1", "127.0.0.1")
    device.online = True
    device.last_seen = at
    device.voltage_v = 224.5
    device.rssi = -52
    for outlet in device.outlets.values():
        outlet.update(on=True, power_w=5.3, energy_kwh=energy, temp_c=31)
    return device.snapshot()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "history.sqlite3"
        self.history = powerk.History(self.path)

    def tearDown(self):
        self.history.close()
        self.folder.cleanup()

    def params(self, **overrides):
        return {"start": 900, "end": 2000, "mac": "", "outlet": None, "bucket": "hour", **overrides}

    def rows(self, params=None):
        content = b"".join(self.history.csv_chunks(params or self.params())).decode()
        return list(csv.DictReader(io.StringIO(content)))

    def test_first_reading_is_baseline_not_historical_consumption(self):
        self.assertTrue(self.history.record(sample(1000, 123.456)))
        report = self.history.report(self.params())
        self.assertEqual(report["summary"]["energy_kwh"], 0)
        self.assertEqual(report["summary"]["readings"], 4)
        for row in self.rows():
            self.assertEqual(row["quality"], "baseline")
            self.assertEqual(row["energy_counter_wh"], "123456")
            self.assertEqual(row["energy_delta_wh"], "")

    def test_saved_samples_and_baseline_survive_restart(self):
        self.history.record(sample(1000))
        self.history = powerk.History(self.path)
        self.assertEqual(self.history.status()["readings_saved"], 4)
        self.assertFalse(self.history.record(sample(1005, 5.001)))
        self.assertTrue(self.history.record(sample(1030, 5.002)))
        self.assertEqual(self.history.report(self.params())["summary"]["energy_kwh"], .008)

    def test_sample_interval_duplicates_and_out_of_order_reports(self):
        self.history.record(sample(1000))
        for at in (1000, 1005, 999):
            self.assertFalse(self.history.record(sample(at)))
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(self.history.record(sample(1030)))
        self.assertEqual(len(self.rows()), 8)

    def test_reset_is_recorded_even_inside_sample_interval_and_never_negative(self):
        self.history.record(sample(1000, 5))
        self.assertTrue(self.history.record(sample(1005, .01)))
        self.history.record(sample(1035, .011))
        report = self.history.report(self.params())
        self.assertEqual(report["summary"]["resets"], 4)
        self.assertEqual(report["summary"]["energy_kwh"], .004)
        resets = [row for row in self.rows() if row["quality"] == "reset"]
        self.assertEqual(len(resets), 4)
        self.assertTrue(all(row["energy_delta_wh"] == "" for row in resets))

    def test_gaps_are_flagged_and_raw_counter_increases_are_preserved(self):
        self.history.record(sample(1000, 5))
        self.history.record(sample(1600, 5.003))
        report = self.history.report(self.params())
        self.assertEqual(report["summary"]["gaps"], 4)
        self.assertEqual(report["summary"]["energy_kwh"], .012)
        gap = self.rows()[-1]
        self.assertEqual(gap["quality"], "gap")
        self.assertEqual(gap["interval_seconds"], "600.0")
        self.assertEqual(gap["energy_delta_wh"], "3")

    def test_range_boundaries_do_not_count_consumption_before_the_period(self):
        self.history.record(sample(1000, 5))
        self.history.record(sample(1030, 5.001))
        self.history.record(sample(1060, 5.002))
        report = self.history.report(self.params(start=1010, end=1060))
        self.assertEqual(report["summary"]["readings"], 4)
        self.assertEqual(report["summary"]["energy_kwh"], 0)
        report = self.history.report(self.params(start=1010, end=1061))
        self.assertEqual(report["summary"]["energy_kwh"], .004)

    def test_bucket_boundary_delta_is_not_falsely_assigned_to_one_hour(self):
        self.history.record(sample(3590, 5))
        self.history.record(sample(3620, 5.001))
        report = self.history.report(self.params(start=3500, end=4000))
        self.assertEqual(report["summary"]["energy_kwh"], .004)
        self.assertEqual(report["summary"]["unallocated_energy_kwh"], .004)
        self.assertEqual(sum(row["energy_kwh"] for row in report["buckets"]), 0)
        self.assertEqual(report["timezone"], "UTC")

    def test_device_and_outlet_filters_do_not_mix_samples(self):
        for mac in (MAC, "112233445566"):
            self.history.record(sample(1000, 5, mac))
            self.history.record(sample(1030, 5.001, mac))
        params = self.params(mac=MAC, outlet=3)
        report = self.history.report(params)
        self.assertEqual(report["summary"]["readings"], 2)
        self.assertEqual(report["summary"]["energy_kwh"], .001)
        self.assertTrue(all(row["mac"] == MAC and row["outlet"] == "3" for row in self.rows(params)))
        self.assertEqual(len(report["devices"]), 2)

    def test_offline_and_incomplete_snapshots_are_not_saved(self):
        offline = sample(1000)
        offline["online"] = False
        self.assertFalse(self.history.record(offline))
        incomplete = sample(1000)
        incomplete["outlets"].pop()
        with self.assertRaises(ValueError):
            self.history.record(incomplete)
        self.assertEqual(self.history.report(self.params())["summary"]["readings"], 0)

    def test_csv_preserves_measurements_units_and_utc_timestamps(self):
        self.history.record(sample(1000))
        row = self.rows()[0]
        self.assertEqual(tuple(row), powerk.History.CSV_FIELDS)
        self.assertTrue(row["timestamp_utc"].endswith("+00:00"))
        self.assertEqual(row["power_w"], "5.3")
        self.assertEqual(row["relay_on"], "1")
        self.assertEqual(row["temperature_c"], "31")
        self.assertEqual(row["voltage_v"], "224.5")
        self.assertEqual(row["rssi_dbm"], "-52")

    def test_streaming_export_does_not_block_writes_or_include_new_samples(self):
        self.history.record(sample(1000))
        chunks = self.history.csv_chunks(self.params())
        header = next(chunks)
        self.assertTrue(self.history.record(sample(1030, 5.001)))
        exported = list(csv.DictReader(io.StringIO((header + b"".join(chunks)).decode())))
        self.assertEqual(len(exported), 4)
        self.assertEqual(len(self.rows()), 8)

    def test_writer_flushes_on_shutdown_and_errors_are_visible(self):
        self.history.start()
        self.history.submit(sample(1000))
        self.history.close()
        self.assertEqual(self.history.status()["readings_saved"], 4)
        self.history.start()
        with patch.object(self.history, "record", side_effect=sqlite3.OperationalError("test disk error")):
            self.history.submit(sample(1030))
            self.history.pending.join()
        self.assertEqual(self.history.status()["failed_snapshots"], 1)
        self.assertIn("write failed", self.history.status()["error"])

    def test_newer_schema_is_rejected_without_rewriting_it(self):
        with sqlite3.connect(str(self.path)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            powerk.History(self.path)
        with sqlite3.connect(str(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_queue_overflow_is_reported_without_blocking_device_handling(self):
        self.history.accepting = True
        for _ in range(129):
            self.history.submit(sample(1000))
        self.assertEqual(self.history.pending.qsize(), 128)
        self.assertEqual(self.history.status()["failed_snapshots"], 1)
        self.assertIn("queue full", self.history.status()["error"])

    def test_invalid_query_parameters_are_rejected(self):
        for query in ("start=nan", "end=inf", "start=-1", "start=200&end=100",
                      "start=1&end=9999999999", "mac=bad", "outlet=0", "outlet=1.5",
                      "bucket=week", "start=1&start=2", "unknown=true"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                powerk.History.parameters(query)
        params = powerk.History.parameters("start=900&end=1100&mac=aabbccddeeff&outlet=4&bucket=hour&t=token")
        self.assertEqual(params["mac"], MAC)
        self.assertEqual(params["outlet"], 4)


if __name__ == "__main__":
    unittest.main()
