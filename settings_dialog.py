import os

from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QPushButton, QFileDialog,
    QDialogButtonBox, QVBoxLayout, QLabel, QCheckBox,
)
from qgis.core import QgsSettings

SETTINGS_GROUP = "gdrive_sync_plugin"
PLUGIN_DIR = os.path.dirname(__file__)
BUNDLED_CLIENT_SECRET = os.path.join(PLUGIN_DIR, "client_secret.json")


def get_setting(key, default=""):
    s = QgsSettings()
    return s.value(f"{SETTINGS_GROUP}/{key}", default)


def set_setting(key, value):
    s = QgsSettings()
    s.setValue(f"{SETTINGS_GROUP}/{key}", value)


def get_client_secret_path():
    """
    Resolves which client secret file to use:
      1. An explicit override the user set in Settings (advanced/rare case)
      2. The file bundled with the plugin (client_secret.json next to this
         file) - this is the normal path, since a Desktop-app OAuth client
         secret is not confidential and is meant to ship with the app.
    """
    override = get_setting("client_secret_json_override", "")
    if override and os.path.exists(override):
        return override
    return BUNDLED_CLIENT_SECRET


class SettingsDialog(QDialog):
    """
    Normal users only fill in:
      - user_id: used to name this user's file, e.g. 'alice'
      - drive_folder_id: the Shared Drive subfolder this user writes to

    The OAuth client secret ships bundled inside the plugin
    (client_secret.json) so end users never have to touch Google Cloud
    Console themselves. An optional advanced override is available for
    anyone who wants to point at a different client secret file.

    On first sync, the user's browser opens once for Google login/consent;
    after that a per-user token is cached on disk (see sync_manager.py)
    and reused silently.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GDrive Spatial Sync - Settings")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.user_id_edit = QLineEdit(get_setting("user_id", ""))
        form.addRow("Your name / User ID (e.g. 'mark'):", self.user_id_edit)

        self.folder_id_edit = QLineEdit(get_setting("drive_folder_id", ""))
        form.addRow("Shared Drive folder ID:", self.folder_id_edit)

        help_label = QLabel(
            "Folder ID = the string after /folders/ in the Shared Drive URL.\n"
            "Your Google account needs Editor/Content Manager access to that\n"
            "Shared Drive folder - ask whoever set it up to share it with you,\n"
            "same as sharing with any teammate."
        )
        help_label.setWordWrap(True)

        layout.addLayout(form)
        layout.addWidget(help_label)

        # Advanced, collapsed-by-default override - most users never touch this.
        self.advanced_toggle = QCheckBox("Advanced: use a different OAuth client secret file")
        self.advanced_toggle.setChecked(bool(get_setting("client_secret_json_override", "")))
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

        self.override_edit = QLineEdit(get_setting("client_secret_json_override", ""))
        self.override_browse_btn = QPushButton("Browse...")
        self.override_browse_btn.clicked.connect(self._browse_key_file)

        adv_form = QFormLayout()
        adv_form.addRow(self.override_edit)
        adv_form.addRow(self.override_browse_btn)
        self.override_edit.setVisible(self.advanced_toggle.isChecked())
        self.override_browse_btn.setVisible(self.advanced_toggle.isChecked())

        layout.addWidget(self.advanced_toggle)
        layout.addLayout(adv_form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_advanced(self, checked):
        self.override_edit.setVisible(checked)
        self.override_browse_btn.setVisible(checked)

    def _browse_key_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OAuth client secret JSON", "", "JSON files (*.json)"
        )
        if path:
            self.override_edit.setText(path)

    def _save_and_close(self):
        set_setting("user_id", self.user_id_edit.text().strip())
        set_setting("drive_folder_id", self.folder_id_edit.text().strip())
        if self.advanced_toggle.isChecked():
            set_setting("client_secret_json_override", self.override_edit.text().strip())
        else:
            set_setting("client_secret_json_override", "")
        self.accept()
