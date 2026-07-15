"""
Google Drive access layer for GDrive Spatial Sync.

Kept in its own module and imported lazily (inside functions, not at
module top level) so a missing dependency doesn't prevent the rest of
the plugin from loading in QGIS - the user just gets a clear error when
they try to sync.

Naming model: each user's data lives under one STABLE filename per
GeoPackage (matching the local file's own name, e.g. "roads.gpkg" or
"<user_id>_working.gpkg"). Every sync checks Drive for that exact name:
if found, its content is replaced in place (Drive keeps automatic
revision history on update); if not found, it's created. There is no
per-sync timestamped versioning - "roads.gpkg" always means "the
current state of roads".

- resolve_user_paths(): finds/creates a per-user subfolder (and a
  "logs" subfolder inside it) under the shared root folder, so each
  user's files live in their own folder instead of all mixed together.
- upload_or_replace(): finds a file with the given exact name in the
  folder and updates its content in place if it exists, else creates
  it. Used for both the main GeoPackage upload and the daily log CSV.
- file_exists_in_folder(): simple exact-name existence check, used by
  the "seed upload if nothing online yet" check.
"""

import os

SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveUploaderError(Exception):
    pass


def _get_credentials(client_secret_json, token_cache_path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise DriveUploaderError(
            "Missing dependency. Install into QGIS's Python with:\n"
            "  <QGIS Python> -m pip install google-api-python-client google-auth "
            "google-auth-oauthlib google-auth-httplib2\n"
            f"Original error: {e}"
        )

    creds = None
    if os.path.exists(token_cache_path):
        try:
            creds = Credentials.from_authorized_user_file(token_cache_path, SCOPES)
        except (ValueError, OSError):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_json):
                raise DriveUploaderError(
                    f"OAuth client secret not found at: {client_secret_json}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_json, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(token_cache_path), exist_ok=True)
        with open(token_cache_path, "w") as f:
            f.write(creds.to_json())

    return creds


def _get_service(client_secret_json, token_cache_path):
    from googleapiclient.discovery import build

    creds = _get_credentials(client_secret_json, token_cache_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file_in_folder(service, folder_id, filename):
    """Returns the file id if a file with this exact name already exists in the folder, else None."""
    safe_name = filename.replace("'", "\\'")
    query = f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false"
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, modifiedTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def find_folder_in_parent(service, parent_id, name):
    """Returns the folder id if a subfolder with this name exists directly under parent_id, else None."""
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{parent_id}' in parents and "
        f"mimeType = '{_FOLDER_MIME}' and trashed = false"
    )
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def get_or_create_subfolder(service, parent_id, name):
    """Finds a subfolder by name under parent_id, creating it if it doesn't exist yet."""
    existing = find_folder_in_parent(service, parent_id, name)
    if existing:
        return existing
    metadata = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
    created = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return created["id"]


def resolve_user_paths(client_secret_json, token_cache_path, root_folder_id, user_id):
    """
    Ensures a per-user folder structure exists under the shared root folder:
        <root_folder_id>/<user_id>/                 <- GeoPackage lands here
        <root_folder_id>/<user_id>/logs/             <- daily edit-log CSVs land here
    Returns (user_folder_id, logs_folder_id).
    """
    service = _get_service(client_secret_json, token_cache_path)
    user_folder_id = get_or_create_subfolder(service, root_folder_id, user_id)
    logs_folder_id = get_or_create_subfolder(service, user_folder_id, "logs")
    return user_folder_id, logs_folder_id


def file_exists_in_folder(client_secret_json, token_cache_path, folder_id, filename):
    """True if a file with this exact name already exists in folder_id."""
    service = _get_service(client_secret_json, token_cache_path)
    return find_file_in_folder(service, folder_id, filename) is not None


def upload_or_replace(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    """
    Finds a file named exactly `remote_filename` in folder_id and updates
    its content in place if it exists, else creates it. This is the only
    upload path used for the main GeoPackage now: same stable filename
    every sync, so "does it exist online -> replace, else create".
    Returns the file's Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service(client_secret_json, token_cache_path)
    media = MediaFileUpload(local_path, resumable=True)

    existing_id = find_file_in_folder(service, folder_id, remote_filename)
    if existing_id:
        updated = service.files().update(
            fileId=existing_id, media_body=media, supportsAllDrives=True,
        ).execute()
        return updated["id"]

    metadata = {"name": remote_filename, "parents": [folder_id]}
    created = service.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return created["id"]


# Kept for backward compatibility with any external caller expecting the
# old name for this exact behaviour.
def upload_file(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    return upload_or_replace(client_secret_json, token_cache_path, folder_id, local_path, remote_filename)
