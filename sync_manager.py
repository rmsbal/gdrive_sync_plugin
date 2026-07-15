import os
import tempfile
import time

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    Qgis, QgsTask, QgsApplication, QgsMessageLog,
    QgsVectorFileWriter, QgsProject, QgsVectorLayer,
    QgsField, QgsFeature, QgsWkbTypes,
)

from .settings_dialog import get_setting, get_client_secret_path
from .drive_uploader import upload_file, DriveUploaderError

PLUGIN_NAME = "GDrive Spatial Sync"

# Simple debounce so a burst of edits (e.g. multi-feature edit + save)
# doesn't trigger several uploads back to back.
_MIN_SECONDS_BETWEEN_SYNCS = 5


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
        folder_id = get_setting("drive_folder_id", "")

        if not user_id or not folder_id:
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "Sync settings incomplete. Open Settings and fill in your "
                "name and the Shared Drive folder ID.",
                level=Qgis.Warning, duration=6,
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
                "The bundled OAuth client secret is still a placeholder. "
                "Whoever set up this plugin needs to replace "
                "client_secret.json with the real file from Google Cloud "
                "Console before syncing will work.",
                level=Qgis.Critical, duration=10,
            )
            return

        remote_filename = f"{user_id}_data.gpkg"
        # Cached login token lives in the user's QGIS profile folder, keyed
        # by user_id, so multiple people on a shared machine don't collide
        # and don't have to re-approve the browser login every sync.
        profile_dir = QgsApplication.qgisSettingsDirPath()
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
            folder_id=folder_id,
            remote_filename=remote_filename,
        )
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    # ------------------------------------------------------------------
    # Runs in a worker thread - must not touch UI widgets directly
    # ------------------------------------------------------------------
    def _layer_safe_for_gpkg(self, layer):
        """
        GeoPackage reserves a primary key column, and by convention
        QGIS/GDAL name it 'fid'. If the source layer already has an
        attribute literally named 'fid' (case-insensitive), GDAL's GPKG
        writer fails with WriterError 5 (ErrAttributeCreationFailed)
        because it can't create a duplicate column.

        This rebuilds the layer in memory with that field renamed to
        'fid_orig', which GDAL can then export cleanly. Returns
        (layer_to_export, note_or_None).
        """
        fields = layer.fields()
        fid_index = -1
        for i, f in enumerate(fields):
            if f.name().lower() == "fid":
                fid_index = i
                break

        if fid_index == -1:
            return layer, None

        wkb_type_str = QgsWkbTypes.displayString(layer.wkbType())
        crs_authid = layer.crs().authid() or "EPSG:4326"
        mem_layer = QgsVectorLayer(
            f"{wkb_type_str}?crs={crs_authid}", layer.name(), "memory"
        )

        new_fields = []
        for f in fields:
            nf = QgsField(f)
            if f.name().lower() == "fid":
                nf.setName("fid_orig")
            new_fields.append(nf)
        mem_layer.dataProvider().addAttributes(new_fields)
        mem_layer.updateFields()

        new_feats = []
        for feat in layer.getFeatures():
            nf = QgsFeature(mem_layer.fields())
            nf.setGeometry(feat.geometry())
            nf.setAttributes(feat.attributes())
            new_feats.append(nf)
        mem_layer.dataProvider().addFeatures(new_feats)

        return mem_layer, "renamed 'fid' field to 'fid_orig' to avoid a GeoPackage reserved-name conflict"

    def _sync_task(self, task, layer, user_id, client_secret_path, token_cache_path,
                    folder_id, remote_filename):
        tmp_dir = tempfile.gettempdir()
        local_path = os.path.join(tmp_dir, f"{user_id}_data_sync.gpkg")

        export_layer, rename_note = self._layer_safe_for_gpkg(layer)

        # Export the (possibly non-gpkg) layer to a GeoPackage snapshot.
        # Works the same way regardless of QGIS 3.x point release.
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = layer.name()

        # QgsVectorFileWriter.writeAsVectorFormatV3 is the modern API
        # (stable since QGIS 3.20ish); fall back to V2 on older installs.
        # Both variants return (WriterError, error_message[, ...]) - the
        # message is what actually explains *why* it failed, so we keep it.
        try:
            transform_context = QgsProject.instance().transformContext()
            err = QgsVectorFileWriter.writeAsVectorFormatV3(
                export_layer, local_path, transform_context, options
            )
        except AttributeError:
            err = QgsVectorFileWriter.writeAsVectorFormatV2(
                export_layer, local_path, QgsProject.instance().transformContext(), options
            )

        if isinstance(err, tuple):
            error_code = err[0]
            error_message = err[1] if len(err) > 1 else ""
        else:
            error_code = err
            error_message = ""

        if error_code != QgsVectorFileWriter.NoError:
            detail = error_message or "no further detail from GDAL"
            if rename_note:
                detail = f"{detail} ({rename_note} was applied but export still failed)"
            return {
                "ok": False,
                "error": f"Export to GeoPackage failed (code {error_code}): {detail}",
            }

        try:
            file_id = upload_file(
                client_secret_path, token_cache_path, folder_id, local_path, remote_filename
            )
        except DriveUploaderError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # network errors, auth errors, etc.
            return {"ok": False, "error": f"Upload failed: {e}"}

        return {"ok": True, "file_id": file_id, "layer_name": layer.name(), "rename_note": rename_note}

    # ------------------------------------------------------------------
    # Runs back on the main thread
    # ------------------------------------------------------------------
    def _sync_finished(self, exception, result=None):
        if exception is not None:
            QgsMessageLog.logMessage(str(exception), PLUGIN_NAME, level=Qgis.Critical)
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Sync error: {exception}", level=Qgis.Critical, duration=6
            )
            return

        if not result or not result.get("ok"):
            err = (result or {}).get("error", "Unknown error")
            QgsMessageLog.logMessage(err, PLUGIN_NAME, level=Qgis.Warning)
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Sync failed: {err}", level=Qgis.Warning, duration=6
            )
            return

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"Synced '{result['layer_name']}' to Google Drive.",
            level=Qgis.Success, duration=4,
        )
        if result.get("rename_note"):
            QgsMessageLog.logMessage(
                f"Note: {result['rename_note']}", PLUGIN_NAME, level=Qgis.Info
            )
