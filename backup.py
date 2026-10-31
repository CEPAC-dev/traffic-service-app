"""Back up the TrafficLens database and the per-client MATLAB workbooks.

Run it by hand or from Windows Task Scheduler / cron:

    python backup.py                 # one backup now, keep the newest 7
    python backup.py --keep 14 --dest D:\\Backups\\trafficlens

The app also calls run_backup() on a schedule (TRAFFICLENS_BACKUP_EVERY_HOURS).
"""

import argparse
import datetime as dt
import os
import sqlite3
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKUP_PREFIX = "trafficlens-backup-"


def default_db_path():
    return Path(os.environ.get("TRAFFICLENS_DB") or APP_DIR / "mobility.db")


def default_runs_dir():
    return APP_DIR / "instance" / "volume_runs"


def default_backup_dir():
    return Path(os.environ.get("TRAFFICLENS_BACKUP_DIR") or APP_DIR / "backups")


def list_backups(dest_dir):
    return sorted(Path(dest_dir).glob(f"{BACKUP_PREFIX}*.zip"))


def latest_backup_time(dest_dir):
    backups = list_backups(dest_dir)
    if not backups:
        return None
    return dt.datetime.fromtimestamp(max(path.stat().st_mtime for path in backups))


def run_backup(db_path=None, runs_dir=None, dest_dir=None, keep=7, now=None):
    """Write one zip with a consistent database snapshot plus every volume_runs file."""
    db_path = Path(db_path or default_db_path())
    runs_dir = Path(runs_dir or default_runs_dir())
    dest_dir = Path(dest_dir or default_backup_dir())
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    snapshot = dest_dir / f".snapshot-{stamp}.sqlite"
    partial = dest_dir / f".{BACKUP_PREFIX}{stamp}.zip.part"
    target = dest_dir / f"{BACKUP_PREFIX}{stamp}.zip"
    try:
        # SQLite's backup API copies a consistent snapshot even while the app is writing.
        source = sqlite3.connect(db_path)
        copy = sqlite3.connect(snapshot)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "database.sqlite")
            if runs_dir.is_dir():
                for path in sorted(runs_dir.rglob("*")):
                    if path.is_file() and not path.name.startswith("."):
                        archive.write(path, f"volume_runs/{path.relative_to(runs_dir).as_posix()}")
        os.replace(partial, target)
    finally:
        for leftover in (snapshot, partial):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
    keep = max(1, int(keep))
    for old in list_backups(dest_dir)[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=None, help="folder for the backup zips (default: ./backups)")
    parser.add_argument("--keep", type=int, default=7, help="how many backups to keep (default: 7)")
    args = parser.parse_args()
    print(run_backup(dest_dir=args.dest, keep=args.keep))


if __name__ == "__main__":
    main()
