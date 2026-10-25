import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook, load_workbook

import volumes_runner as runner


class VolumeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workbook_path = self.root / "Inquiries_12-10-2026.xlsx"
        self.models_path = self.root / "Models.mat"
        self.models_path.write_bytes(b"test model placeholder")
        self.run_root = self.root / "app"
        self.run_root.mkdir()
        self.create_workbook()
        self.patches = [
            mock.patch.object(runner, "APP_DIR", self.run_root),
            mock.patch.object(runner, "INPUT_TEMPLATE", self.workbook_path),
            mock.patch.object(runner, "MODELS_FILE", self.models_path),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp_dir.cleanup()

    def create_workbook(self, rows=None, sheet_name="Inqueries", headers=None):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = sheet_name
        sheet.append(headers or ["R", "Lanes", "FFS", "Actual Speed"])
        for row in rows or []:
            sheet.append(row)
        workbook.save(self.workbook_path)
        workbook.close()

    def read_rows(self, path, sheet_name="Inqueries"):
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            return list(workbook[sheet_name].iter_rows(values_only=True))
        finally:
            workbook.close()

    def request_rows(self):
        return [
            {"hour": 8, "lanes": 2, "freeflow_speed": 40, "actual_speed": 25.5},
            {"hour": 9, "lanes": 2, "freeflow_speed": 40, "actual_speed": 28.0},
        ]

    def test_prepare_persists_hourly_rows_and_replaces_only_this_request(self):
        self.create_workbook(rows=[
            [7, 1, 35, 22],
            [42, 3, 55, 40],
            [7, 2, 38, 21],
        ])

        prepared = runner.prepare_volume_run(7, self.request_rows())

        shared_rows = self.read_rows(self.workbook_path)
        self.assertEqual(shared_rows[0], ("R", "Lanes", "FFS", "Actual Speed"))
        self.assertEqual(shared_rows[1], (42, 3, 55, 40))
        self.assertEqual(shared_rows[2:], [
            (7, 2, 40, 25.5),
            (7, 2, 40, 28),
        ])
        self.assertEqual(prepared["hours"], [8, 9])
        self.assertEqual(
            self.read_rows(prepared["input_workbook"]),
            [
                ("R", "Lanes", "FFS", "Actual Speed", "Hour"),
                (7, 2, 40, 25.5, 8),
                (7, 2, 40, 28, 9),
            ],
        )
        self.assertFalse(
            self.workbook_path.with_name("~$" + self.workbook_path.name).exists()
        )

    def test_excel_lock_prevents_any_write(self):
        before = self.workbook_path.read_bytes()
        lock_path = self.workbook_path.with_name("~$" + self.workbook_path.name)
        lock_path.touch()
        try:
            with self.assertRaisesRegex(RuntimeError, "open in Excel"):
                runner.prepare_volume_run(7, self.request_rows())
        finally:
            lock_path.unlink()
        self.assertEqual(self.workbook_path.read_bytes(), before)
        self.assertFalse((self.run_root / "instance" / "volume_runs").exists())

    def test_missing_sheet_and_wrong_headers_are_rejected_without_mutation(self):
        self.create_workbook(sheet_name="Other")
        before = self.workbook_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Inqueries sheet"):
            runner.prepare_volume_run(7, self.request_rows())
        self.assertEqual(self.workbook_path.read_bytes(), before)

        self.create_workbook(headers=["Lanes", "R", "FFS", "Actual Speed"])
        before = self.workbook_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "must begin with columns"):
            runner.prepare_volume_run(7, self.request_rows())
        self.assertEqual(self.workbook_path.read_bytes(), before)

    def test_missing_workbook_and_invalid_values_are_rejected(self):
        self.workbook_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "workbook is missing"):
            runner.prepare_volume_run(7, self.request_rows())
        self.assertFalse((self.run_root / "instance" / "volume_runs").exists())

        self.create_workbook(rows=[[42, 3, 55, 40]])
        before = self.workbook_path.read_bytes()
        invalid_rows = self.request_rows()
        invalid_rows[0]["lanes"] = 0
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            runner.prepare_volume_run(7, invalid_rows)
        self.assertEqual(self.workbook_path.read_bytes(), before)

    def test_unsorted_hour_inputs_are_staged_in_hour_order(self):
        rows = list(reversed(self.request_rows()))
        prepared = runner.prepare_volume_run(7, rows)
        staged = self.read_rows(prepared["input_workbook"])
        self.assertEqual(prepared["hours"], [8, 9])
        self.assertEqual(staged[0][4], "Hour")
        self.assertEqual([row[3] for row in staged[1:]], [25.5, 28])
        self.assertEqual([row[4] for row in staged[1:]], [8, 9])

    def test_excel_float_round_trip_precision_does_not_fail_write_verification(self):
        precise_speed = 29.420689655172414
        prepared = runner.prepare_volume_run(7, [
            {
                "hour": 8,
                "lanes": 1.0,
                "freeflow_speed": precise_speed,
                "actual_speed": precise_speed,
            },
        ])
        self.assertEqual(prepared["hours"], [8])
        self.assertAlmostEqual(
            self.read_rows(self.workbook_path)[1][2],
            precise_speed,
            places=12,
        )

    def test_output_without_hour_values_is_mapped_to_current_request_hours(self):
        prepared = runner.prepare_volume_run(7, self.request_rows())

        def fake_matlab(*args, **kwargs):
            result_path = Path(prepared["result_workbook"])
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Volumes"
            sheet.append([
                "R", "Lanes", "FFS", "Actual Speed", "Hour",
                "Estimated Volume", "RMSE Tolerance",
            ])
            sheet.append([7, 2, 40, 25.5, None, 100, 5])
            sheet.append([7, 2, 40, 28, None, 120, 6])
            workbook.save(result_path)
            workbook.close()
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(runner, "_matlab_executable", return_value="matlab"), \
                mock.patch.object(runner.subprocess, "run", side_effect=fake_matlab) as matlab:
            result = runner.run_prepared_volume_estimation(prepared)

        self.assertEqual([item["hour"] for item in result["hourly"]], [8, 9])
        self.assertEqual([item["value"] for item in result["hourly"]], [100, 120])
        self.assertEqual(matlab.call_count, 1)
        self.assertEqual(result["shared_workbook"], str(self.workbook_path))

    def test_matlab_failure_is_reported(self):
        prepared = runner.prepare_volume_run(7, self.request_rows())
        failed_run = mock.Mock(returncode=1, stdout="", stderr="model error")
        with mock.patch.object(runner, "_matlab_executable", return_value="matlab"), \
                mock.patch.object(runner.subprocess, "run", return_value=failed_run):
            with self.assertRaisesRegex(RuntimeError, "MATLAB volume calculation failed.*model error"):
                runner.run_prepared_volume_estimation(prepared)

    def test_matching_saved_matlab_result_is_reused_without_running_matlab(self):
        prepared = runner.prepare_volume_run(7, self.request_rows())
        result_book = Workbook()
        sheet = result_book.active
        sheet.title = "Volumes"
        sheet.append([
            "R", "Lanes", "FFS", "Actual Speed", "Hour",
            "Estimated Volume", "RMSE Tolerance",
        ])
        sheet.append([7, 2, 40, 25.5, 8, 120.5, 6.2])
        sheet.append([7, 2, 40, 28, 9, 140.75, 7.1])
        result_book.save(prepared["result_workbook"])
        result_book.close()

        with mock.patch.object(runner.subprocess, "run") as matlab:
            result = runner.find_existing_volume_result(7, self.request_rows())

        self.assertIsNotNone(result)
        self.assertTrue(result["reused_existing_result"])
        self.assertEqual(result["hourly"], [
            {"hour": 8, "value": 120.5, "tolerance": 6.2},
            {"hour": 9, "value": 140.75, "tolerance": 7.1},
        ])
        matlab.assert_not_called()

    def test_saved_matlab_result_is_not_reused_for_different_inputs(self):
        prepared = runner.prepare_volume_run(7, self.request_rows())
        result_book = Workbook()
        sheet = result_book.active
        sheet.title = "Volumes"
        sheet.append([
            "R", "Lanes", "FFS", "Actual Speed", "Hour",
            "Estimated Volume", "RMSE Tolerance",
        ])
        sheet.append([7, 2, 40, 25.5, 8, 120.5, 6.2])
        sheet.append([7, 2, 40, 28, 9, 140.75, 7.1])
        result_book.save(prepared["result_workbook"])
        result_book.close()

        changed_rows = self.request_rows()
        changed_rows[0]["actual_speed"] = 26.0
        self.assertIsNone(runner.find_existing_volume_result(7, changed_rows))


if __name__ == "__main__":
    unittest.main()
