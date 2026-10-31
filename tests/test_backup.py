import datetime as dt
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import backup


class BackupTests(unittest.TestCase):
    def make_database(self, root):
        db_path = root / "app.db"
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE sample(value INTEGER)")
        connection.execute("INSERT INTO sample VALUES (42)")
        connection.commit()
        connection.close()
        return db_path

    def test_backup_holds_a_database_snapshot_and_run_files_and_prunes_old_ones(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = self.make_database(root)
            runs = root / "runs"
            (runs / "client-1" / "REQ-1").mkdir(parents=True)
            (runs / "client-1" / "REQ-1" / "result.xlsx").write_bytes(b"workbook")
            dest = root / "backups"

            created = [
                backup.run_backup(db_path, runs, dest, keep=2, now=dt.datetime(2026, 10, day, 10, 0, 0))
                for day in (1, 2, 3)
            ]

            remaining = backup.list_backups(dest)
            self.assertEqual(remaining, created[1:])
            self.assertEqual([path.name for path in dest.iterdir() if path.name.startswith(".")], [])
            with zipfile.ZipFile(created[-1]) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    ["database.sqlite", "volume_runs/client-1/REQ-1/result.xlsx"],
                )
                restored = root / "restored.sqlite"
                restored.write_bytes(archive.read("database.sqlite"))
            connection = sqlite3.connect(restored)
            self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], 42)
            connection.close()
            self.assertIsNotNone(backup.latest_backup_time(dest))

    def test_missing_database_is_reported_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(FileNotFoundError):
                backup.run_backup(root / "missing.db", root / "runs", root / "backups")
            self.assertEqual(backup.list_backups(root / "backups"), [])


if __name__ == "__main__":
    unittest.main()
