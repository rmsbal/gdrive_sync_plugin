"""
Google Drive access layer for GDrive Spatial Sync.

Kept in its own module and imported lazily (inside functions, not at
module top level) so a missing dependency doesn't prevent the rest of
the plugin from loading in QGIS - the user just gets a clear error when
they try to sync.

New in this revision:
- resolve_user_paths(): finds/creates a per-user subfolder (and a
  "logs" subfolder inside it) under the shared root folder, so each
  user's files live in their own folder instead of all mixed together.
- unique_remote_name(): never silently overwrites. If a file with the
  same name already exists in the target folder, a "-1", "-2", ...
  suffix is added instead, so a naming collision produces a new
  version rather than clobbering someone else's (or an earlier) file.
- upload_new_version(): always creates a new Drive file (used for the
  timestamped GeoPackage uploads - each sync is its own version).
- upload_or_replace(): finds-and-updates a file with an exact name if
  it exists, else creates it (used for the daily log CSV, which is
  meant to accumulate under one stable filename per day).
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
    query = (
        f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false"
    )
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

    metadata = {
        "name": name,
        "mimeType": _FOLDER_MIME,
        "parents": [parent_id],
    }
    created = service.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def resolve_user_paths(client_secret_json, token_cache_path, root_folder_id, user_id):
    """
    Ensures a per-user folder structure exists under the shared root folder:

        <root_folder_id>/
          <user_id>/
            <user_id's GeoPackage versions land here>
            logs/
              <user_id's daily edit-log CSVs land here>

    Returns (user_folder_id, logs_folder_id).
    """
    service = _get_service(client_secret_json, token_cache_path)
    user_folder_id = get_or_create_subfolder(service, root_folder_id, user_id)
    logs_folder_id = get_or_create_subfolder(service, user_folder_id, "logs")
    return user_folder_id, logs_folder_id


def unique_remote_name(client_secret_json, token_cache_path, folder_id, filename):
    """
    Returns a filename guaranteed not to collide with an existing file in
    folder_id. If `filename` is already free, it's returned unchanged.
    Otherwise "-1", "-2", ... is inserted before the extension until a
    free name is found - so a conflict produces a new version file
    instead of overwriting whatever is already there.
    """
    service = _get_service(client_secret_json, token_cache_path)
    return _unique_remote_name_with_service(service, folder_id, filename)


def _unique_remote_name_with_service(service, folder_id, filename):
    if find_file_in_folder(service, folder_id, filename) is None:
        return filename

    base, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = f"{base}-{n}{ext}"
        if find_file_in_folder(service, folder_id, candidate) is None:
            return candidate
        n += 1


def has_uploaded_version(client_secret_json, token_cache_path, folder_id, base_name):
    """
    True if a Drive file whose name starts with '<base_name>_v' already
    exists in folder_id - i.e. this local GeoPackage has already been
    synced online at least once, under any timestamped version name.

    Used for the "upload automatically if not found online yet" check
    when a GeoPackage is opened/used in the project, so a file already
    present on Drive isn't re-uploaded just because it was reloaded.
    """
    service = _get_service(client_secret_json, token_cache_path)
    safe_base = base_name.replace("'", "\\'")
    query = (
        f"name contains '{safe_base}_v' and '{folder_id}' in parents and trashed = false"
    )
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return bool(response.get("files"))


def upload_new_version(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    """
    Always creates a brand-new Drive file (never updates an existing one).
    Used for the timestamped GeoPackage uploads: each sync is its own
    version, so a name collision is resolved by versioning the name
    rather than by overwriting, and this call assumes `remote_filename`
    has already been made unique via unique_remote_name().

    Returns the new file's Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service(client_secret_json, token_cache_path)
    media = MediaFileUpload(local_path, resumable=True)
    metadata = {"name": remote_filename, "parents": [folder_id]}
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def upload_or_replace(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    """
    Finds a file named exactly `remote_filename` in folder_id and updates
    its content in place if it exists, else creates it. Used for the
    daily edit-log CSV, which is meant to accumulate under one stable
    filename per day rather than spawn a new file per sync.

    Returns the file's Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service(client_secret_json, token_cache_path)
    media = MediaFileUpload(local_path, resumable=True)

    existing_id = find_file_in_folder(service, folder_id, remote_filename)
    if existing_id:
        updated = service.files().update(
            fileId=existing_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        return updated["id"]

    metadata = {"name": remote_filename, "parents": [folder_id]}
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


# Kept for backward compatibility with any external caller expecting the
# old single-file "upload or overwrite" behaviour.
def upload_file(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    """
    Legacy helper: uploads local_path as remote_filename, overwriting any
    existing file of that exact name in folder_id (Drive keeps automatic
    revision history on update). Returns the file ID.
    """
    return upload_or_replace(
        client_secret_json, token_cache_path, folder_id, local_path, remote_filename
    )
