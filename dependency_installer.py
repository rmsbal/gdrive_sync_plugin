"""
Checks whether the Google API libraries this plugin needs are installed
in QGIS's own Python environment, and if not, offers to install them
automatically via pip - so non-developer users never have to open a
terminal or OSGeo4W shell themselves.
"""

import sys
import subprocess
import importlib.util

from qgis.PyQt.QtWidgets import QMessageBox
from qgis.core import Qgis, QgsTask, QgsApplication, QgsMessageLog

PLUGIN_NAME = "GDrive Spatial Sync"


def _exec_dialog(dialog):
    """Qt5 uses exec_(), Qt6 renamed it to exec(). Handle both QGIS builds."""
    return dialog.exec_() if hasattr(dialog, "exec_") else dialog.exec()


# (import name, pip package name) - import name is what we test for,
# pip name is what gets installed since they sometimes differ.
REQUIRED_PACKAGES = [
    ("googleapiclient", "google-api-python-client"),
    ("google.auth", "google-auth"),
    ("google_auth_oauthlib", "google-auth-oauthlib"),
    ("google.auth.transport.requests", None),  # part of google-auth, no separate install
]


def _is_importable(module_name):
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def get_missing_packages():
    """Returns a de-duplicated list of pip package names that need installing."""
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES:
        if pip_name is None:
            continue
        if not _is_importable(import_name) and pip_name not in missing:
            missing.append(pip_name)
    return missing


class DependencyInstaller:
    """
    Owns the "check on startup -> ask user -> install in background ->
    report result" flow. Instantiate once from the main plugin class.
    """

    def __init__(self, iface):
        self.iface = iface
        self._task = None

    def check_and_offer_install(self):
        missing = get_missing_packages()
        if not missing:
            return

        msg = QMessageBox(self.iface.mainWindow())
        msg.setWindowTitle(PLUGIN_NAME)
        msg.setIcon(QMessageBox.Question)
        msg.setText("GDrive Spatial Sync needs some additional components.")
        msg.setInformativeText(
            "The following will be installed automatically into QGIS's "
            "Python environment:\n\n  " + "\n  ".join(missing) +
            "\n\nThis needs an internet connection and may take a minute. "
            "Install now?"
        )
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.Yes)

        if _exec_dialog(msg) == QMessageBox.Yes:
            self._install(missing)

    def _install(self, packages):
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, "Installing required components in the background...",
            level=Qgis.Info, duration=4,
        )

        task = QgsTask.fromFunction(
            "Installing GDrive Spatial Sync dependencies",
            self._install_task,
            on_finished=self._install_finished,
            packages=packages,
        )
        self._task = task
        QgsApplication.taskManager().addTask(task)

    # Runs in a worker thread.
    def _install_task(self, task, packages):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", *packages],
                capture_output=True, text=True, timeout=600,
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr[-1500:] or "pip install failed"}
        return {"ok": True, "log": result.stdout[-500:]}

    # Runs back on the main thread.
    def _install_finished(self, exception, result=None):
        if exception is not None:
            QgsMessageLog.logMessage(str(exception), PLUGIN_NAME, level=Qgis.Critical)
            self._show_failure(str(exception))
            return

        if not result or not result.get("ok"):
            err = (result or {}).get("error", "Unknown error")
            QgsMessageLog.logMessage(err, PLUGIN_NAME, level=Qgis.Critical)
            self._show_failure(err)
            return

        still_missing = get_missing_packages()
        if still_missing:
            self._show_failure(
                "Install finished but these are still missing: " + ", ".join(still_missing)
            )
            return

        QMessageBox.information(
            self.iface.mainWindow(), PLUGIN_NAME,
            "Required components installed successfully.\n\n"
            "Please restart QGIS once before using the plugin, so the "
            "newly installed libraries are picked up."
        )

    def _show_failure(self, detail):
        msg = QMessageBox(self.iface.mainWindow())
        msg.setWindowTitle(PLUGIN_NAME)
        msg.setIcon(QMessageBox.Critical)
        msg.setText("Automatic install failed.")
        msg.setInformativeText(
            "You can install manually instead. Open the OSGeo4W Shell "
            "(Windows) or a terminal where QGIS runs, and paste:\n\n"
            "python -m pip install google-api-python-client google-auth "
            "google-auth-oauthlib google-auth-httplib2"
        )
        msg.setDetailedText(detail)
        _exec_dialog(msg)
