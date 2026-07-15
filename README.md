# GDrive Spatial Sync (QGIS Plugin)

Auto-syncs an edited layer to a **per-user GeoPackage** on an organizational
Google Drive (Shared Drive). Each user writes only to their own file, so
there are no write conflicts.

Compatible with QGIS **3.16 through the latest 3.x / 4.x** builds, on
Windows, macOS, and Linux — it only uses `qgis.PyQt` (auto-resolves
PyQt5/PyQt6) and QGIS core APIs that have been stable since 3.16.

Uses **OAuth2 (each user logs in as themselves)** rather than a service
account key, since many Google Workspace orgs now block service account
key creation via org policy. No admin exception needed for this approach.

The OAuth client secret is **bundled inside the plugin** — end users
never touch Google Cloud Console. Only one person (whoever sets this up)
does the Cloud Console steps once; everyone else just installs the
plugin and enters their name + a folder ID.

## 1. Required libraries — bundled in the plugin, nothing to install

This plugin ships with all required Google API libraries already
included in the `libs/` folder — you do **not** need pip, a terminal,
or an internet connection on the end user's machine at plugin-install
time. `__init__.py` automatically adds `libs/` to the Python path.

**Important — platform target:** the bundled compiled components
(`cryptography`, `cffi`, `charset_normalizer`) were built for
**64-bit Windows, Python 3.12** (the Python version bundled with
current QGIS Windows installers). This covers the vast majority of
current QGIS setups. If your QGIS reports a different Python version,
see "If the bundled libraries don't match your QGIS" below.

**To check your QGIS's Python version:** Plugins → Python Console:
```python
import sys
print(sys.version)
```

<details>
<summary>If the bundled libraries don't match your QGIS (wrong Python version/OS)</summary>

Delete the `libs/` folder contents and either:
- Re-run the plugin's auto-install prompt (installs into QGIS's own
  Python via pip instead of using the bundled copy), or
- Manually install:
  ```
  python -m pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
  ```
  in the OSGeo4W Shell (Windows) or a terminal where `qgis` runs
  (macOS/Linux).
</details>

---

## 2. One-time admin setup (only one person needs to do this)

1. Google Cloud Console → sign in with your org account → new/existing
   project → enable **Google Drive API** (APIs & Services → Library).
2. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
   - If prompted, configure the **OAuth consent screen** first: User Type
     "Internal" (Workspace orgs, recommended if available - logs in
     automatically for anyone in your org) or "External" (add each
     teammate's email as a **Test user** under Audience), app name
     anything, no special scopes needed on that screen.
   - Application type: **Desktop app** → Create.
   - Download the JSON.
   - This is *not* a service account key, so it isn't blocked by the
     `iam.disableServiceAccountKeyCreation` org policy, and it's safe to
     bundle inside the plugin - Google treats Desktop-app client secrets
     as non-confidential by design.
3. **Replace** the placeholder `client_secret.json` file in this plugin
   folder with the file you just downloaded (keep the filename
   `client_secret.json`).
4. In Google Drive, open your organization's **Shared Drive** → create/choose
   a folder → **Share** it with each teammate's own Google account (normal
   sharing, like sharing with any person) → role: **Content Manager** or
   **Editor**.
5. Copy the folder ID from the URL: `.../folders/<THIS_PART>`.
6. Zip the whole plugin folder (with your real `client_secret.json`
   inside it now) and distribute that zip to your team.

## 3. Install the plugin (everyone)

1. QGIS → Plugins → Manage and Install Plugins → Install from ZIP →
   select the zip from step 2.6 above.

## 4. Configure (everyone)

Plugin menu → **GDrive Spatial Sync → Settings**:
- **Your name / User ID**: short unique name, e.g. `mark` (used as the
  filename prefix - each person gets their own file, so there are never
  write conflicts).
- **Shared Drive folder ID**: from the admin, step 2.5 above.

That's it — no client secret field, no JSON key, no Cloud Console.

## 5. Use

- Select a layer → **GDrive Spatial Sync → Enable auto-sync on save** to
  upload automatically every time you commit edits.
- Or click the toolbar **Sync current layer now** button any time.
- **First sync only:** a browser window opens asking you to log into
  Google and approve access. After that, a cached login token is stored

  locally (in your QGIS profile folder) and reused silently — no repeat
  logins needed.

Each sync exports the active layer to a GeoPackage and uploads it as
`<user_id>_data.gpkg` inside the configured folder, overwriting the
previous version. Google Drive keeps automatic revision history, so
prior versions are recoverable from Drive's "Version history" on that file.

## Notes / next steps

- This skeleton overwrites the whole file per sync — fine for typical
  survey/field-data layer sizes. For very large layers, this is a good
  place to add incremental diff-based updates.
- The client secret path is stored in `QgsSettings` (per-user QGIS
  profile, not synced anywhere); the login token cache lives alongside
  it in the profile folder. For stricter environments, swap this for
  QGIS's `QgsAuthManager` identity store.
- No retry queue is included yet — a failed sync just shows a warning
  and can be re-triggered manually or on the next edit. Add a local
  SQLite queue in `sync_manager.py` if you need offline resilience.
