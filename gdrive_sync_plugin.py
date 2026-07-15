"""
GDrive Spatial Sync - main plugin class.

Only uses APIs that have been stable across QGIS 3.16 -> 3.4x -> 4.x:
- qgis.PyQt.* (auto-resolves to PyQt5 or PyQt6 under the hood)
- QgsProject / QgsVectorLayer signal API (stable since QGIS 3.x)
- QgsTask / QgsApplication.taskManager() (available since 3.0)

Auto-sync now has two separate triggers:

1. Seed upload (new) - the moment a GeoPackage-backed layer (or a
   layer that will live in this user's working GeoPackage) is opened
   or added to the project, the plugin checks Drive for an existing
   uploaded version. If none is found, it uploads one automatically -
   so a brand-new file doesn't sit local-only until someone remembers
   to edit it or save the project.

2. Save-triggered sync (changed) - any layer the user has toggled
   "Enable auto-sync on save" for is uploaded whenever the QGIS
   *project* is saved, not on every individual edit commit. This
   replaces the old committedFeaturesAdded/committedGeometriesChanges/
   etc. wiring, which fired on every edit-commit rather than on
   project save.
"""

import os
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject, Qgis, QgsMessageLog, QgsVectorLayer

from .settings_dialog import SettingsDialog
from .sync_manager import SyncManager
from .dependency_installer import DependencyInstaller, get_missing_packages

PLUGIN_NAME = "GDrive Spatial Sync"


class GDriveSyncPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = PLUGIN_NAME
        self.toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self.toolbar.setObjectName(PLUGIN_NAME)

        self.sync_manager = SyncManager(iface)
        self.dependency_installer = DependencyInstaller(iface)
        self.settings_dialog = None

        self._watched_layer_ids = set()       # layers to sync on project save
        self._seed_checked_layer_ids = set()  # layers already seed-checked this session

    # ------------------------------------------------------------------
    # QGIS plugin lifecycle
    # ------------------------------------------------------------------
    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.sync_action = QAction(icon, "Sync current layer now", self.iface.mainWindow())
        self.sync_action.triggered.connect(self.sync_now)
        self.toolbar.addAction(self.sync_action)
        self.iface.addPluginToMenu(self.menu, self.sync_action)
        self.actions.append(self.sync_action)

        self.settings_action = QAction("Settings...", self.iface.mainWindow())
        self.settings_action.triggered.connect(self.open_settings)
        self.iface.addPluginToMenu(self.menu, self.settings_action)
        self.actions.append(self.settings_action)

        self.toggle_watch_action = QAction("Enable auto-sync on save", self.iface.mainWindow())
        self.toggle_watch_action.setCheckable(True)
        self.toggle_watch_action.toggled.connect(self.toggle_auto_sync)
        self.iface.addPluginToMenu(self.menu, self.toggle_watch_action)
        self.actions.append(self.toggle_watch_action)

        # Used for the seed-upload check: run it for every layer
        # already loaded, and for any added later.
        QgsProject.instance().layersAdded.connect(self._on_layers_added)

        # This now drives recurring auto-sync for watched layers,
        # replacing the old per-edit-commit trigger.
        QgsProject.instance().projectSaved.connect(self._on_project_saved)

        # Seed-check layers already in the project when the plugin
        # finishes loading (e.g. the project was opened before the
        # plugin was ready).
        QTimer.singleShot(1000, self._seed_check_existing_layers)

        # Check for missing libraries shortly after startup (not
        # immediately, so the toolbar/menu finish loading first) and
        # offer to auto-install them - no terminal needed from the user.
        QTimer.singleShot(1500, self.dependency_installer.check_and_offer_install)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.toolbar.removeAction(action)
        del self.toolbar

        try:
            QgsProject.instance().layersAdded.disconnect(self._on_layers_added)
        except TypeError:
            pass
        try:
            QgsProject.instance().projectSaved.disconnect(self._on_project_saved)
        except TypeError:
            pass

        self._watched_layer_ids.clear()
        self._seed_checked_layer_ids.clear()

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def open_settings(self):
        self.settings_dialog = SettingsDialog(self.iface.mainWindow())
        self.settings_dialog.exec_() if hasattr(self.settings_dialog, "exec_") else self.settings_dialog.exec()

    def sync_now(self):
        layer = self.iface.activeLayer()
        if layer is None:
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_NAME, "No active layer selected.")
            return
        if get_missing_packages():
            self.dependency_installer.check_and_offer_install()
            return
        self.sync_manager.sync_layer(layer, manual=True)

    def toggle_auto_sync(self, checked):
        layer = self.iface.activeLayer()
        if layer is None:
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_NAME, "No active layer selected.")
            self.toggle_watch_action.setChecked(False)
            return

        if checked:
            self._watched_layer_ids.add(layer.id())
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME,
                f"Auto-sync enabled for '{layer.name()}' - it will upload whenever you save the project.",
                level=Qgis.Info, duration=4,
            )
        else:
            self._watched_layer_ids.discard(layer.id())
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Auto-sync disabled for '{layer.name()}'", level=Qgis.Info, duration=4
            )

    # ------------------------------------------------------------------
    # Trigger 1: project save -> sync every watched layer
    # ------------------------------------------------------------------
    def _on_project_saved(self):
        if not self._watched_layer_ids:
            return
        if get_missing_packages():
            QgsMessageLog.logMessage(
                "Skipping auto-sync on save: required libraries not installed yet.",
                PLUGIN_NAME, level=Qgis.Warning,
            )
            return

        project = QgsProject.instance()
        for layer_id in list(self._watched_layer_ids):
            layer = project.mapLayer(layer_id)
            if layer is None:
                # Layer no longer exists in the project; stop watching it.
                self._watched_layer_ids.discard(layer_id)
                continue
            QgsMessageLog.logMessage(
                f"Project saved - queuing sync for watched layer '{layer.name()}'.",
                PLUGIN_NAME, level=Qgis.Info,
            )
            self.sync_manager.sync_layer(layer, manual=False)

    # ------------------------------------------------------------------
    # Trigger 2: layer opened/used in the project -> seed upload once,
    # only if this user has no uploaded version online yet
    # ------------------------------------------------------------------
    def _on_layers_added(self, layers):
        for layer in layers:
            self._maybe_seed_upload(layer)

    def _seed_check_existing_layers(self):
        for layer in QgsProject.instance().mapLayers().values():
            self._maybe_seed_upload(layer)

    def _maybe_seed_upload(self, layer):
        if not isinstance(layer, QgsVectorLayer):
            return
        if layer.id() in self._seed_checked_layer_ids:
            return
        self._seed_checked_layer_ids.add(layer.id())

        if get_missing_packages():
            QgsMessageLog.logMessage(
                f"Skipping seed-upload check for '{layer.name()}': required libraries not installed yet.",
                PLUGIN_NAME, level=Qgis.Warning,
            )
            return

        self.sync_manager.seed_upload_if_missing(layer)
