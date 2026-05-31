# UploadPilot

UploadPilot is a local Python + Playwright tool that uploads a folder of files to a YouLearn space one-by-one and renames each uploaded item back to its queued title. It exposes a small FastAPI web UI at `http://127.0.0.1:8765/` for starting runs and watching live progress.

## Quick start

1. Install Python 3.11+ on Windows.
2. Double-click `scripts\setup.bat`. This creates `.venv`, installs dependencies, downloads Chromium for Playwright, and creates `config.yaml` from the example.
3. Copy `.env.example` to `.env` and fill in `YOULERN_EMAIL` and `YOULERN_PASSWORD`.
   To enable Google Drive sync, also set `VITE_GOOGLE_CLIENT_ID` to a Google OAuth Web client ID whose Authorized JavaScript origins include your app URL.
4. Double-click `scripts\install_autostart.bat`. This drops a hidden shortcut in your Startup folder so the server launches on every login, and starts it now.
5. Bookmark `http://127.0.0.1:8765/` in your browser. That's your single-click entry point.

## Login method

- The uploader signs in to YouLearn with the email/password stored in `.env`.
- Uploads run with `browser.headless: true` by default, so Chromium does not open a visible window.
- Temporarily set `browser.headless: false` in `config.yaml` only when debugging login or selector changes.
- The persistent profile in `.browser_profile/` is still used for cookies/session reuse, but the app can re-authenticate automatically when YouLearn sends you back to login.

## Using it

1. Open `http://127.0.0.1:8765/`.
2. Paste the space URL, for example `https://app.youlearn.ai/space/d9e3441968ab4d4d`.
3. Paste the local folder path, for example `D:\courses\ethics`.
4. Click `Preview` to confirm the file list and order.
5. Click `Start upload`. The table shows live status and a per-video progress bar for each file: `queued -> uploading -> settling -> renaming -> validating -> uploaded`.

Files are uploaded one at a time. The worker waits for each upload to appear in the space listing before moving on, then renames it back to the original queued title. Nested files are scanned recursively; their queued titles include the relative folder path.

## Rename recovery

UploadPilot keeps a local recovery ledger in `.state/upload_history.json` and syncs that ledger through the private Drive payload when Google Drive sync is enabled. Each uploaded row stores the source path, target YouLearn title, YouLearn's generated `youlearn_ai_name`, latest known YouLearn title, content URL when available, space URL/path, status, and upload/rename/validation timestamps. If the app or machine stops after upload but before rename, reopen the dashboard and use the row-level `Continue rename` button. If a video later has the wrong title, use `Retry naming` on that row to re-check YouLearn and apply the target title again. If the uploaded item itself needs to be recreated, use `Retry upload` on that row to upload the saved local file again and run the settle/rename flow for the new item.

## Configuration

Edit `config.yaml` (created by setup). Useful knobs:

- `server.port` - default `8765`
- `browser.headless` - default `true` for hidden uploads; set `false` only for debugging
- `uploads.extensions` - which file types to include; defaults include `.txt`, `.pdf`, and common video formats
- `uploads.sort` - upload order (`name_asc` by default)
- `youlearn.login_url` - login page used for email/password auth (`https://app.youlearn.ai/signin`)
- `youlearn.list_poll_timeout_s` - how long to wait for a video to appear after upload
- `VITE_GOOGLE_CLIENT_ID` - Google Identity Services OAuth client ID for private Drive `appDataFolder` sync

## Files

- `uploader/youlearn.py` - page selectors and login automation
- `uploader/worker.py` - per-job loop; upload -> detect new row -> rename
- `uploader/server.py` - FastAPI routes and SSE event stream
- `logs/uploader.log` - runtime log file

## Troubleshooting

- `YouLearn credentials are missing` - create `.env` from `.env.example` and set `YOULERN_EMAIL` / `YOULERN_PASSWORD`
- `Nothing happens on Start` - check `logs\uploader.log`. If the server is not running, rerun `scripts\install_autostart.bat`.
- `Login selector broke` - update the `SEL_SIGN_IN_*`, `SEL_EMAIL_INPUT`, or `SEL_PASSWORD_INPUT` constants in `uploader/youlearn.py`
- `Upload selector broke` - update the relevant `SEL_*` constants in `uploader/youlearn.py`
- `Stop the server` - `scripts\uninstall_autostart.bat` removes the startup shortcut and stops the background process
