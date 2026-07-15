"""
GDrive Spatial Sync
Entry point required by QGIS. Kept deliberately tiny.

If the plugin folder contains a 'libs' subfolder (created by running
`pip install --target=./libs <packages>` before packaging), it's added
to sys.path here so the vendored Google API libraries can be imported
without the end user needing to install anything themselves.
"""

import os
import sys

_PLUGIN_DIR = os.path.dirname(__file__)
_LIBS_DIR = os.path.join(_PLUGIN_DIR, "libs")

if os.path.isdir(_LIBS_DIR) and _LIBS_DIR not in sys.path:
    # Insert at the front so a vendored copy takes priority over any
    # older version that might already be installed in QGIS's Python.
    sys.path.insert(0, _LIBS_DIR)


def classFactory(iface):
    from .gdrive_sync_plugin import GDriveSyncPlugin
    return GDriveSyncPlugin(iface)
