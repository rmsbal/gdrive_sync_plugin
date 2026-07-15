"""
Thin wrapper around the Google Drive API v3.

Uses OAuth2 "installed app" (Desktop) flow with per-user token caching,
instead of a service account key - because many Workspace orgs now
block service account key creation via org policy
(iam.disableServiceAccountKeyCreation). Each user authenticates as
themselves once; after that, a cached token is reused silently.

Kept in its own module and imported lazily (inside functions, not at
module top level) so a missing dependency doesn't prevent the rest of
the plugin from loading in QGIS - the user just gets a clear error when
they try to sync.
"""

import os

SCOPES = ["https://www.googleapis.com/auth/drive"]


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

    # Reuse a cached token from a previous login, if present and valid.
    if os.path.exists(token_cache_path):
        try:
            creds = Credentials.from_authorized_user_file(token_cache_path, SCOPES)
        except Exception:
            creds = None  # corrupt/old cache - fall through to re-login

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_cache_path)
            return creds
        except Exception:
            creds = None  # refresh failed - fall through to interactive login

    if not os.path.exists(client_secret_json):
        raise DriveUploaderError(
            f"OAuth client secret file not found: {client_secret_json}"
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_json, SCOPES)
        # Opens the user's default browser for a one-time login/consent.
        # Blocks the calling thread until the user finishes in the browser -
        # this runs inside the background QgsTask, so it will NOT freeze
        # the QGIS UI, but the sync will visibly pause until they approve.
        creds = flow.run_local_server(port=0)
    except Exception as e:
        raise DriveUploaderError(f"OAuth login failed: {e}")

    _save_token(creds, token_cache_path)
    return creds


def _save_token(creds, token_cache_path):
    os.makedirs(os.path.dirname(token_cache_path), exist_ok=True)
    with open(token_cache_path, "w") as f:
        f.write(creds.to_json())


def _get_service(client_secret_json, token_cache_path):
    from googleapiclient.discovery import build

    creds = _get_credentials(client_secret_json, token_cache_path)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file_in_folder(service, folder_id, filename):
    """Returns the file id if a file with this name already exists in the folder, else None."""
    query = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
    response = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, modifiedTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def upload_file(client_secret_json, token_cache_path, folder_id, local_path, remote_filename):
    """
    Uploads local_path to the given Shared Drive folder as remote_filename,
    authenticating as the logged-in user (OAuth2, not a service account).
    Creates the file if it doesn't exist, otherwise updates it in place
    (Drive keeps automatic revision history on update).
    Returns the file ID.
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service(client_secret_json, token_cache_path)
    media = MediaFileUpload(local_path, mimetype="application/geopackage+sqlite3", resumable=True)

    existing_id = find_file_in_folder(service, folder_id, remote_filename)

    if existing_id:
        updated = service.files().update(
            fileId=existing_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        return updated["id"]
    else:
        metadata = {"name": remote_filename, "parents": [folder_id]}
        created = service.files().create(
            body=metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return created["id"]
