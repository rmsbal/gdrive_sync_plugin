"""
Sync orchestration for GDrive Spatial Sync.

Runs each sync as a background QgsTask so QGIS's UI thread never blocks
on a Drive upload, and throttles auto-triggered syncs so a burst of edit
commits doesn't trigger several uploads back to back.

Sync flow (see sync_layer / _sync_task):
  1. Resolve this user's Drive subfolder (and its "logs" subfolder)
     under the shared root folder configured in Settings - creating
     them the first time this user syncs.
  2. Figure out which local GeoPackage to upload:
       - If the active layer is already backed by a .gpkg file on
         disk, that file *is* the upload - nothing is exported.
       - Otherwise, reuse (or create) this user's local "working"
         GeoPackage and merge in any project layer not already saved
         to it, as its own table, leaving existing tables untouched.
     The local working-file path and the set of layers already merged
     into it are remembered per user (state_store.py) so this never
     changes just because the *remote* filename is versioned per sync.
  3. Upload the GeoPackage to Drive under a versioned name:
       <local_basename>_v<YYYYMMDD>_<HHMMSS>.gpkg
     using a 24-hour clock. If that exact name somehow already exists
     in the folder (e.g. two syncs in the same second), a numeric
     suffix is added instead of overwriting, so a conflict always
     produces a new version rather than clobbering a file.
  4. Append a row to today's local CSV edit log for this user and
     push that file to their Drive "logs" folder (same filename all
     day, updated in place) so a reviewer can open one file to check
     a user's activity for a given day.
"""

import os
import re
import time
from datetime import datetime

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    Qgis, QgsTask, QgsApplication, QgsMessageLog,
    QgsVectorFileWriter, QgsProject, QgsVectorLayer,
    QgsField, QgsFeature, QgsWkbTypes,
)

from .settings_dialog import get_setting, get_client_secret_path
from .drive_uploader import (
    DriveUploaderError, resolve_user_paths, unique_remote_name,
    upload_new_version, upload_or_replace,
)
from . import state_store
from . import edit_logger

PLUGIN_NAME = "GDrive Spatial Sync"

# Throttles auto-sync (commit-triggered) calls so a rapid string of
# edits doesn't trigger several uploads back to back. Manual syncs
# (the toolbar button) always run regardless of this.
_MIN_SECONDS_BETWEEN_SYNCS = 5


def _profile_dir():
    return QgsApplication.qgisSettingsDirPath()


def _safe_table_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
    return cleaned or "layer"


def _is_gpkg_backed(layer):
    """True if this vector layer's data already lives in a .gpkg file on disk."""
    if not isinstance(layer, QgsVectorLayer):
        return False
    if layer.providerType() != "ogr":
        return False
    path = layer.source().split("|")[0]
    return path.lower().endswith(".gpkg") and os.path.exists(path)


def _gpkg_source_path(layer):
    return layer.source().split("|")[0]


def _list_gpkg_tables(path):
    """Table (layer) names already present in a GeoPackage file, or an empty set if unreadable."""
    if not path or not os.path.exists(path):
        return set()
    try:
        from osgeo import ogr
    except ImportError:
        return set()
    ds = ogr.Open(path)
    if ds is None:
        return set()
    names = set()
    for i in range(ds.GetLayerCount()):
        names.add(ds.GetLayer(i).GetName())
    ds = None
    return names


def _prepare_layer_for_gpkg(layer):
    """
    GeoPackage reserves a primary key column, and by convention
    QGIS/GDAL name it 'fid'. If the source layer already has an
    attribute literally named 'fid' (case-insensitive), GDAL's GPKG
    writer fails with WriterError 5 (ErrAttributeCreationFailed).

    Works around this by copying the layer into an in-memory layer
    with that field renamed to 'fid_orig' before writing. Returns
    (layer_to_write, rename_note) where rename_note is None if no
    rename was needed.
    """
    fields = layer.fields()
    fid_index = fields.lookupField("fid")
    if fid_index == -1:
        return layer, None

    mem_layer = QgsVectorLayer(
        f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}",
        layer.name(),
        "memory",
    )
    mem_provider = mem_layer.dataProvider()

    new_fields = []
    for f in fields:
        nf = QgsField(f)
        if nf.name().lower() == "fid":
            nf.setName("fid_orig")
        new_fields.append(nf)
    mem_provider.addAttributes(new_fields)
    mem_layer.updateFields()

    new_feats = []
    for feat in layer.getFeatures():
        nf = QgsFeature(mem_layer.fields())
        nf.setGeometry(feat.geometry())
        nf.setAttributes(feat.attributes())
        new_feats.append(nf)
    mem_provider.addFeatures(new_feats)

    return mem_layer, "renamed 'fid' field to 'fid_orig' to avoid a GeoPackage reserved-name conflict"


class SyncManager:
    def __init__(self, iface):
        self.iface = iface
        self._last_sync_time = {}  # layer_id -> timestamp
        self._tasks = []  # keep references so QgsTask isn't garbage collected mid-run

    @staticmethod
    def _is_placeholder_client_secret(path):
        try:
            with open(path, "r") as f:
                content = f.read()
            return "REPLACE_ME" in content
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def sync_layer(self, layer, manual=False):
        if layer is None:
            return

        now = time.time()
        last = self._last_sync_time.get(layer.id(), 0)
        if not manual and (now - last) < _MIN_SECONDS_BETWEEN_SYNCS:
            return
        self._last_sync_time[layer.id()] = now

        user_id = get_setting("user_id", "")
        client_secret_path = get_client_secret_path()
        root_folder_id = get_setting("drive_folder_id", "")

        if not user_id or not root_folder_id:
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "Sync settings incomplete. Open Settings and fill in your "
                "User ID and Shared Drive folder ID.",
                level=Qgis.Critical, duration=8,
            )
            return

        if not os.path.exists(client_secret_path):
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "No OAuth client secret found. This plugin should ship with "
                "one bundled - reinstall the plugin, or set an override path "
                "in Settings (Advanced).",
                level=Qgis.Critical, duration=8,
            )
            return

        if self._is_placeholder_client_secret(client_secret_path):
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "The bundled client secret is a placeholder. Replacing "
                "client_secret.json with the real file from Google Cloud "
                "Console before syncing will work.",
                level=Qgis.Critical, duration=10,
            )
            return

        profile_dir = _profile_dir()
        token_cache_path = os.path.join(
            profile_dir, "gdrive_sync_plugin", f"{user_id}_token.json"
        )

        task = QgsTask.fromFunction(
            f"Sync '{layer.name()}' to Google Drive",
            self._sync_task,
            on_finished=self._sync_finished,
            layer=layer,
            user_id=user_id,
            client_secret_path=client_secret_path,
            token_cache_path=token_cache_path,
            root_folder_id=root_folder_id,
            profile_dir=profile_dir,
        )
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    # ------------------------------------------------------------------
    # Background worker (runs off the main thread)
    # ------------------------------------------------------------------
    def _ensure_working_gpkg(self, profile_dir, user_id, trigger_layer):
        """
        Returns the path to this user's local working GeoPackage,
        creating it if needed and merging in any project vector layer
        that isn't already its own separate .gpkg file and hasn't
        already been written into this working file.
        """
        state = state_store.load_state(profile_dir, user_id)
        local_path = state.get("local_gpkg_path")

        if not local_path or not os.path.exists(local_path):
            working_dir = os.path.join(profile_dir, "gdrive_sync_plugin", "working")
            os.makedirs(working_dir, exist_ok=True)
            local_path = os.path.join(working_dir, f"{user_id}_working.gpkg")
            state["local_gpkg_path"] = local_path
            state["layers_in_gpkg"] = []

        merged_identities = set(state.get("layers_in_gpkg", []))
        existing_tables = _list_gpkg_tables(local_path)

        candidates = [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and not _is_gpkg_backed(lyr)
        ]
        # Sync the layer that triggered this first, then any others.
        candidates.sort(key=lambda l: 0 if l.id() == trigger_layer.id() else 1)

        file_exists_on_disk = os.path.exists(local_path)

        for lyr in candidates:
            identity = state_store.layer_identity(lyr)
            if identity in merged_identities:
                continue

            table_name = _safe_table_name(lyr.name())
            suffix = 1
            base_table_name = table_name
            while table_name in existing_tables:
                suffix += 1
                table_name = f"{base_table_name}_{suffix}"

            layer_to_write, rename_note = _prepare_layer_for_gpkg(lyr)
            if rename_note:
                QgsMessageLog.logMessage(
                    f"'{lyr.name()}': {rename_note}", PLUGIN_NAME, level=Qgis.Info
                )

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            options.layerName = table_name
            options.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteLayer
                if file_exists_on_disk
                else QgsVectorFileWriter.CreateOrOverwriteFile
            )

            error, err_msg, new_path, new_layer = QgsVectorFileWriter.writeAsVectorFormatV2(
                layer_to_write,
                local_path,
                QgsProject.instance().transformContext(),
                options,
            )

            if error != QgsVectorFileWriter.NoError:
                QgsMessageLog.logMessage(
                    f"Failed to add layer '{lyr.name()}' to working GeoPackage "
                    f"(code {error}): {err_msg}",
                    PLUGIN_NAME, level=Qgis.Warning,
                )
                continue

            file_exists_on_disk = True
            existing_tables.add(table_name)
            merged_identities.add(identity)

        state["layers_in_gpkg"] = list(merged_identities)
        state_store.save_state(profile_dir, user_id, state)
        return local_path

    def _sync_task(self, task, layer, user_id, client_secret_path, token_cache_path,
                    root_folder_id, profile_dir):
        when = datetime.now()

        # 1. Which local GeoPackage are we uploading?
        if _is_gpkg_backed(layer):
            local_path = _gpkg_source_path(layer)
            state = state_store.load_state(profile_dir, user_id)
            state["local_gpkg_path"] = local_path
            state_store.save_state(profile_dir, user_id, state)
            action = "upload_existing_gpkg"
        else:
            local_path = self._ensure_working_gpkg(profile_dir, user_id, layer)
            action = "merge_and_upload_working_gpkg"

        if not os.path.exists(local_path):
            detail = f"Local GeoPackage not found at {local_path}"
            self._log_attempt(profile_dir, user_id, layer.name(), action, "", "failed", detail, when)
            return {"error": detail}

        # 2. Per-user Drive folder (and its logs subfolder).
        try:
            user_folder_id, logs_folder_id = resolve_user_paths(
                client_secret_path, token_cache_path, root_folder_id, user_id
            )
        except DriveUploaderError as e:
            detail = str(e)
            self._log_attempt(profile_dir, user_id, layer.name(), action, "", "failed", detail, when)
            return {"error": detail}

        # 3. Versioned remote filename: <basename>_vYYYYMMDD_HHMMSS.gpkg (24h clock).
        base_name = os.path.splitext(os.path.basename(local_path))[0]
        timestamp = when.strftime("%Y%m%d_%H%M%S")
        candidate_name = f"{base_name}_v{timestamp}.gpkg"

        try:
            remote_filename = unique_remote_name(
                client_secret_path, token_cache_path, user_folder_id, candidate_name
            )
        except DriveUploaderError as e:
            detail = str(e)
            self._log_attempt(profile_dir, user_id, layer.name(), action, candidate_name, "failed", detail, when)
            return {"error": detail}

        # 4. Upload as a brand-new version - never overwrite an existing file.
        try:
            upload_new_version(
                client_secret_path, token_cache_path, user_folder_id, local_path, remote_filename
            )
        except DriveUploaderError as e:
            detail = str(e)
            self._log_attempt(profile_dir, user_id, layer.name(), action, remote_filename, "failed", detail, when)
            return {"error": detail}

        # 5. Daily edit log: one row locally, then push today's log file to Drive.
        self._log_attempt(profile_dir, user_id, layer.name(), action, remote_filename, "success", "", when)
        try:
            local_log_path = edit_logger.local_log_path(profile_dir, user_id, when)
            remote_log_name = edit_logger.remote_log_filename(user_id, when)
            upload_or_replace(
                client_secret_path, token_cache_path, logs_folder_id, local_log_path, remote_log_name
            )
        except DriveUploaderError as e:
            QgsMessageLog.logMessage(
                f"Uploaded '{remote_filename}' but failed to push the daily log: {e}",
                PLUGIN_NAME, level=Qgis.Warning,
            )

        return {
            "layer_name": layer.name(),
            "remote_filename": remote_filename,
            "user_id": user_id,
        }

    def _log_attempt(self, profile_dir, user_id, layer_name, action, remote_filename,
                      status, detail, when):
        try:
            edit_logger.append_log_row(
                profile_dir, user_id, layer_name, action, remote_filename, status, detail, when
            )
        except OSError as e:
            QgsMessageLog.logMessage(
                f"Failed to write local edit log: {e}", PLUGIN_NAME, level=Qgis.Warning
            )

    # ------------------------------------------------------------------
    # Runs back on the main thread
    # ------------------------------------------------------------------
    def _sync_finished(self, exception, result=None):
        if exception is not None:
            QgsMessageLog.logMessage(
                f"Sync task raised an exception: {exception}", PLUGIN_NAME, level=Qgis.Critical
            )
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, "Sync failed - see log for details.", level=Qgis.Critical, duration=6
            )
            return

        if not result:
            return

        if "error" in result:
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, result["error"], level=Qgis.Critical, duration=8
            )
            return

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"Synced '{result['layer_name']}' as {result['remote_filename']}.",
            level=Qgis.Success, duration=5,
        )
