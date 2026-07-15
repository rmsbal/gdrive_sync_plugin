"""
Small local persistence layer for GDrive Spatial Sync.

Remembers, per user, which local GeoPackage acts as that user's working
file and which layers have already been written into it - so changing
the *remote* upload naming scheme (filename_vYYYYMMDD_HHMMSS.gpkg) never
causes a new local working file to be created, and a layer already
merged into the GeoPackage is never re-added as a duplicate table.

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
    "layers_in_gpkg": [],      # layer identities already merged into it
    "last_remote_filename": None,
}


def load_state(profile_dir, user_id):
    path = _state_path(profile_dir, user_id)
    if not os.path.exists(path):
        return dict(_DEFAULT_STATE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    merged = dict(_DEFAULT_STATE)
    merged.update(data)
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
    """
    return f"{layer.name()}::{layer.source()}"
