"""
Small local persistence layer for GDrive Spatial Sync.

Remembers, per user:
  - which local GeoPackage is that user's stable "working" file
  - which table name each merged-in layer was given, so a layer that's
    already in the working GeoPackage gets its EXISTING table
    overwritten with current data on every sync, instead of either
    being skipped (stale data) or added again as a duplicate table.

State lives under the QGIS user profile, not inside the project file,
so it survives across projects and QGIS restarts:
    <profile>/gdrive_sync_plugin/state/<user_id>_state.json
"""

import os
import json

_STATE_SUBDIR = os.path.join("gdrive_sync_plugin", "state")


def _safe_user(user_id):
    cleaned = "".join(c for c in user_id if c.isalnum() or c in ("-", "_"))
    return cleaned or "user"


def _state_dir(profile_dir):
    d = os.path.join(profile_dir, _STATE_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(profile_dir, user_id):
    return os.path.join(_state_dir(profile_dir), f"{_safe_user(user_id)}_state.json")


_DEFAULT_STATE = {
    "local_gpkg_path": None,   # this user's stable local working GeoPackage
    "layer_tables": {},        # layer_identity -> table_name already used for it
}


def load_state(profile_dir, user_id):
    path = _state_path(profile_dir, user_id)
    if not os.path.exists(path):
        return {"local_gpkg_path": None, "layer_tables": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}

    merged = dict(_DEFAULT_STATE)
    merged["layer_tables"] = {}
    merged.update(data)

    # Migrate the old "layers_in_gpkg" list format (identity only, no
    # remembered table name) - those layers will just get a fresh table
    # name assigned once on the next sync, which is safe.
    if "layer_tables" not in data and "layers_in_gpkg" in data:
        merged["layer_tables"] = {}

    return merged


def save_state(profile_dir, user_id, state):
    path = _state_path(profile_dir, user_id)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


def layer_identity(layer):
    """
    A stable-ish key for 'has this layer already been merged into the
    working GeoPackage', independent of the layer's random QGIS layer
    id (which changes every time the project is reopened).

    Note: this changes if the layer is renamed in QGIS or its source
    path changes - in that case it's treated as a "new" layer and gets
    its own table rather than updating the previous one.
    """
    return f"{layer.name()}::{layer.source()}"
