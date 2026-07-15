import os

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProject, Qgis, QgsMessageLog

from .settings_dialog import SettingsDialog
from .sync_manager import SyncManager
from .dependency_installer import DependencyInstaller, get_missing_packages

PLUGIN_NAME = "GDrive Spatial Sync"


class GDriveSyncPlugin:
    """
    Main plugin class. Only uses APIs that have been stable across
    QGIS 3.16 -> 3.4x -> 4.x:
      - qgis.PyQt.* (auto-resolves to PyQt5 or PyQt6 under the hood)
      - QgsProject / QgsVectorLayer signal API (stable since QGIS 3.x)
      - QgsTask / QgsApplication.taskManager() (available since 3.0)
    """

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

        self._watched_layer_ids = set()

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

        # Hook into layers already loaded, and any added later
        QgsProject.instance().layersAdded.connect(self._on_layers_added)

        # Check for missing libraries shortly after startup (not
        # immediately, so the toolbar/menu finish loading first) and
        # offer to auto-install them - no terminal needed from the user.
        from qgis.PyQt.QtCore import QTimer
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

        self._unwatch_all_layers()

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
            self._watch_layer(layer)
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Auto-sync enabled for '{layer.name()}'", level=Qgis.Info, duration=4
            )
        else:
            self._unwatch_layer(layer)
            self.iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Auto-sync disabled for '{layer.name()}'", level=Qgis.Info, duration=4
            )

    # ------------------------------------------------------------------
    # Layer watching (commit-based, not per-keystroke)
    # ------------------------------------------------------------------
    def _watch_layer(self, layer):
        if layer.id() in self._watched_layer_ids:
            return
        layer.committedFeaturesAdded.connect(self._make_commit_handler(layer))
        layer.committedGeometriesChanges.connect(self._make_commit_handler(layer))
        layer.committedAttributeValuesChanges.connect(self._make_commit_handler(layer))
        layer.committedFeaturesRemoved.connect(self._make_commit_handler(layer))
        self._watched_layer_ids.add(layer.id())

    def _unwatch_layer(self, layer):
        # Signals are dropped automatically when the layer is deleted;
        # this covers the explicit "user turned it off" case.
        for signal_name in (
            "committedFeaturesAdded",
            "committedGeometriesChanges",
            "committedAttributeValuesChanges",
            "committedFeaturesRemoved",
        ):
            try:
                getattr(layer, signal_name).disconnect()
            except TypeError:
                pass
        self._watched_layer_ids.discard(layer.id())

    def _unwatch_all_layers(self):
        for layer_id in list(self._watched_layer_ids):
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                self._unwatch_layer(layer)

    def _make_commit_handler(self, layer):
        def handler(*args, **kwargs):
            if get_missing_packages():
                # Don't spam a popup on every edit - just log it, since
                # the startup check already offered installation once.
                QgsMessageLog.logMessage(
                    "Skipping auto-sync: required libraries not installed yet.",
                    PLUGIN_NAME, level=Qgis.Warning,
                )
                return
            QgsMessageLog.logMessage(
                f"Detected committed change on '{layer.name()}', queuing sync.",
                PLUGIN_NAME, level=Qgis.Info,
            )
            self.sync_manager.sync_layer(layer, manual=False)
        return handler

    def _on_layers_added(self, layers):
        # No-op hook point if you want to auto-watch new layers by
        # naming convention, project variable, etc.
        pass
