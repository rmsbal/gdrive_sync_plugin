"""
Daily edit-log helper for GDrive Spatial Sync.

Writes one CSV row per sync attempt (success or failure) to a local
per-day log file, then that file gets uploaded/updated in place (same
filename all day, one row appended per sync) so a reviewer can open a
single file on Drive to verify a user's activity for a given day.

Local log files live under the QGIS profile, one folder per user:
  <profile>/gdrive_sync_plugin/logs/<user_id>/<user_id>_editlog_YYYYMMDD.csv

Columns:
  timestamp, user_id, layer_name, action, remote_filename, status, detail
"""

import os
import csv
from datetime import datetime

_LOG_SUBDIR = os.path.join("gdrive_sync_plugin", "logs")

_FIELDNAMES = [
    "timestamp", "user_id", "layer_name", "action",
    "remote_filename", "status", "detail",
]


def _safe_user(user_id):
    cleaned = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    return cleaned or "user"


def _local_log_dir(profile_dir, user_id):
    d = os.path.join(profile_dir, _LOG_SUBDIR, _safe_user(user_id))
    os.makedirs(d, exist_ok=True)
    return d


def local_log_path(profile_dir, user_id, when=None):
    when = when or datetime.now()
    day_str = when.strftime("%Y%m%d")
    fname = f"{_safe_user(user_id)}_editlog_{day_str}.csv"
    return os.path.join(_local_log_dir(profile_dir, user_id), fname)


def remote_log_filename(user_id, when=None):
    when = when or datetime.now()
    day_str = when.strftime("%Y%m%d")
    return f"{_safe_user(user_id)}_editlog_{day_str}.csv"


def append_log_row(profile_dir, user_id, layer_name, action, remote_filename,
                    status, detail="", when=None):
    """
    Appends one row to today's local CSV log for this user, creating the
    file (with header) if it doesn't exist yet. Returns the local path
    written to, so the caller can upload/update it on Drive right after.
    """
    when = when or datetime.now()
    path = local_log_path(profile_dir, user_id, when)
    file_exists = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": when.strftime("%Y-%m-%d %H:%M:%S"),
            "user_id": user_id,
            "layer_name": layer_name,
            "action": action,
            "remote_filename": remote_filename,
            "status": status,
            "detail": detail,
        })

    return path
