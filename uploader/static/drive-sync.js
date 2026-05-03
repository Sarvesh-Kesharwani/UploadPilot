(() => {
  const DRIVE_SCOPE = [
    "https://www.googleapis.com/auth/drive.appdata",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
  ].join(" ");
  const DRIVE_API = "https://www.googleapis.com/drive/v3";
  const DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3";
  const JSON_MIME = "application/json";
  const FOLDER_MIME = "application/vnd.google-apps.folder";
  const SESSION_KEY = "uploadpilot_google_session";

  const state = {
    config: null,
    tokenClient: null,
    accessToken: "",
    expiresAt: 0,
    profile: null,
    hydrationPending: false,
    autosaveTimer: 0,
    silentRefreshTried: false,
    onStatus: () => {},
    getData: () => ({}),
    applyData: () => {},
    setEditBlocked: () => {},
  };

  function init(options) {
    Object.assign(state, options);
    state.config = options.config;
    state.onStatus({ signedIn: false, message: "Drive signed out" });
    waitForGis().then(() => {
      if (!state.config.googleClientId) {
        state.onStatus({ signedIn: false, error: "Missing OAuth client ID. Set VITE_GOOGLE_CLIENT_ID." });
        return;
      }
      state.tokenClient = google.accounts.oauth2.initTokenClient({
        client_id: state.config.googleClientId,
        scope: DRIVE_SCOPE,
        callback: onToken,
        error_callback: () => state.onStatus({ signedIn: false, error: "Google sign-in failed" }),
      });
      restoreSessionMeta();
    }).catch(() => state.onStatus({ signedIn: false, error: "Google Identity Services failed to load" }));
  }

  function signIn() {
    if (!state.tokenClient) {
      state.onStatus({ signedIn: false, error: "Missing OAuth client ID. Set VITE_GOOGLE_CLIENT_ID." });
      return;
    }
    state.tokenClient.requestAccessToken({ prompt: "consent" });
  }

  function signOut() {
    if (state.accessToken && window.google?.accounts?.oauth2) {
      google.accounts.oauth2.revoke(state.accessToken, () => {});
    }
    state.accessToken = "";
    state.expiresAt = 0;
    state.profile = null;
    localStorage.removeItem(SESSION_KEY);
    state.setEditBlocked(false);
    state.onStatus({ signedIn: false, message: "Drive signed out" });
  }

  async function onToken(tokenResponse) {
    if (tokenResponse.error) {
      state.onStatus({ signedIn: false, error: tokenResponse.error_description || tokenResponse.error });
      return;
    }
    state.accessToken = tokenResponse.access_token;
    state.expiresAt = Date.now() + Number(tokenResponse.expires_in || 3600) * 1000 - 60000;
    state.silentRefreshTried = false;
    try {
      state.profile = await driveFetch("https://www.googleapis.com/oauth2/v3/userinfo");
      localStorage.setItem(SESSION_KEY, JSON.stringify({
        profile: state.profile,
        savedAt: Date.now(),
      }));
      state.onStatus({ signedIn: true, profile: state.profile, message: "Loading Drive state..." });
      await hydrateFromDrive();
      scheduleSave();
    } catch (err) {
      state.onStatus({ signedIn: true, profile: state.profile, error: err.message });
    }
  }

  async function hydrateFromDrive() {
    requireSignedIn();
    state.hydrationPending = true;
    state.setEditBlocked(true);
    try {
      const primary = await findPrimary();
      if (primary) {
        const payload = await downloadJson(primary.id);
        validatePayload(payload);
        state.applyData(payload.data || {});
        state.onStatus({ signedIn: true, profile: state.profile, message: "Drive state loaded" });
      } else {
        await saveNow();
        state.onStatus({ signedIn: true, profile: state.profile, message: "New Drive sync file created" });
      }
    } finally {
      state.hydrationPending = false;
      state.setEditBlocked(false);
    }
  }

  function scheduleSave() {
    if (!state.accessToken || state.hydrationPending) return;
    clearTimeout(state.autosaveTimer);
    state.autosaveTimer = setTimeout(() => saveNow().catch((err) => {
      state.onStatus({ signedIn: true, profile: state.profile, error: err.message });
    }), 1200);
  }

  async function saveNow() {
    requireSignedIn();
    await ensureFreshToken();
    const payload = makePayload();
    const primary = await findPrimary();
    if (primary) {
      let previous = null;
      try {
        previous = await downloadJson(primary.id);
        validatePayload(previous);
      } catch (err) {
        throw new Error(`Invalid Drive payload; refusing overwrite: ${err.message}`);
      }
      await createBackups(previous);
      await updateJson(primary.id, state.config.primarySyncFile, payload);
    } else {
      await createJson(state.config.primarySyncFile, payload, ["appDataFolder"]);
    }
    state.onStatus({ signedIn: true, profile: state.profile, message: `Saved ${new Date().toLocaleTimeString()}` });
    return payload;
  }

  async function loadFromDrive() {
    await hydrateFromDrive();
  }

  async function fetchRestoreInfo() {
    requireSignedIn();
    await ensureFreshToken();
    return apiFetch("/api/drive/restore", { method: "GET" });
  }

  async function restore(date) {
    requireSignedIn();
    await ensureFreshToken();
    const result = await apiFetch("/api/drive/restore", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(date ? { date } : {}),
    });
    validatePayload(result.restored);
    state.applyData(result.restored.data || {});
    return result;
  }

  async function apiFetch(url, init = {}) {
    const res = await fetch(url, {
      ...init,
      headers: {
        ...(init.headers || {}),
        authorization: `Bearer ${state.accessToken}`,
      },
    });
    if (res.status === 401) throw new Error("Signed out or expired token");
    if (res.status === 404) throw new Error("No backup found");
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  async function createBackups(previousPayload) {
    try {
      const folder = await ensureBackupFolder();
      const today = localDate(0);
      const dailyName = `${state.config.appName}-state-${today}.json`;
      if (!(await findFile(dailyName, `'${folder.id}' in parents and trashed = false`))) {
        await createJson(dailyName, previousPayload, [folder.id]);
      }
      const snapshotName = `${state.config.appName}-snapshot-${new Date().toISOString().replace(/:/g, "-")}.json`;
      await createJson(snapshotName, previousPayload, [folder.id]);
    } catch (err) {
      console.warn("Backup failed before overwrite; continuing primary sync", err);
      state.onStatus({ signedIn: true, profile: state.profile, message: "Backup failed; primary sync continued" });
    }
  }

  async function ensureBackupFolder() {
    const existing = await findBackupFolder();
    if (existing) return existing;
    const res = await driveFetch(`${DRIVE_API}/files?fields=id,name`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        name: state.config.backupFolder,
        mimeType: FOLDER_MIME,
        parents: ["appDataFolder"],
      }),
    });
    return res;
  }

  async function findPrimary() {
    return findFile(state.config.primarySyncFile, "trashed = false");
  }

  async function findBackupFolder() {
    return findFile(state.config.backupFolder, `mimeType = '${FOLDER_MIME}' and trashed = false`);
  }

  async function findFile(name, extraQuery) {
    const q = `name = '${escapeQuery(name)}' and ${extraQuery}`;
    const params = new URLSearchParams({
      spaces: "appDataFolder",
      q,
      fields: "files(id,name,createdTime,modifiedTime)",
      pageSize: "10",
    });
    const data = await driveFetch(`${DRIVE_API}/files?${params}`);
    return data.files?.[0] || null;
  }

  async function downloadJson(fileId) {
    return driveFetch(`${DRIVE_API}/files/${fileId}?alt=media`);
  }

  async function createJson(name, payload, parents) {
    const { body, contentType } = multipart({ name, mimeType: JSON_MIME, parents }, payload);
    return driveFetch(`${DRIVE_UPLOAD_API}/files?uploadType=multipart&fields=id,name,createdTime`, {
      method: "POST",
      headers: { "content-type": contentType },
      body,
    });
  }

  async function updateJson(fileId, name, payload) {
    const { body, contentType } = multipart({ name, mimeType: JSON_MIME }, payload);
    return driveFetch(`${DRIVE_UPLOAD_API}/files/${fileId}?uploadType=multipart&fields=id,name,modifiedTime`, {
      method: "PATCH",
      headers: { "content-type": contentType },
      body,
    });
  }

  async function driveFetch(url, init = {}) {
    await ensureFreshToken();
    const res = await fetch(url, {
      ...init,
      headers: {
        ...(init.headers || {}),
        authorization: `Bearer ${state.accessToken}`,
      },
    });
    if (res.status === 401) throw new Error("Expired token");
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  async function ensureFreshToken() {
    if (state.accessToken && Date.now() < state.expiresAt) return;
    if (!state.tokenClient || state.silentRefreshTried) {
      throw new Error("Expired token. Sign in again.");
    }
    state.silentRefreshTried = true;
    await new Promise((resolve, reject) => {
      const oldCallback = state.tokenClient.callback;
      state.tokenClient.callback = (res) => {
        state.tokenClient.callback = oldCallback;
        if (res.error) reject(new Error("Expired token. Sign in again."));
        else {
          state.accessToken = res.access_token;
          state.expiresAt = Date.now() + Number(res.expires_in || 3600) * 1000 - 60000;
          resolve();
        }
      };
      state.tokenClient.requestAccessToken({ prompt: "" });
    });
  }

  function requireSignedIn() {
    if (!state.accessToken) throw new Error("Signed out: Drive API blocked");
  }

  function makePayload() {
    return {
      app: state.config.appName,
      schemaVersion: 1,
      savedAt: Date.now(),
      data: state.getData(),
    };
  }

  function validatePayload(payload) {
    if (!payload || payload.app !== state.config.appName || payload.schemaVersion !== 1 || !("savedAt" in payload) || !("data" in payload)) {
      throw new Error("Invalid Drive payload");
    }
  }

  function multipart(metadata, payload) {
    const boundary = "uploadpilot_boundary";
    const body = [
      `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n`,
      JSON.stringify(metadata),
      `\r\n--${boundary}\r\nContent-Type: ${JSON_MIME}\r\n\r\n`,
      JSON.stringify(payload),
      `\r\n--${boundary}--\r\n`,
    ].join("");
    return { body, contentType: `multipart/related; boundary=${boundary}` };
  }

  function localDate(offsetDays) {
    const date = new Date(Date.now() + offsetDays * 86400000);
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: state.config.timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(date);
    const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
    return `${map.year}-${map.month}-${map.day}`;
  }

  function escapeQuery(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  }

  function waitForGis() {
    return new Promise((resolve, reject) => {
      const started = Date.now();
      const timer = setInterval(() => {
        if (window.google?.accounts?.oauth2) {
          clearInterval(timer);
          resolve();
        } else if (Date.now() - started > 10000) {
          clearInterval(timer);
          reject(new Error("GIS load timeout"));
        }
      }, 100);
    });
  }

  function restoreSessionMeta() {
    try {
      const saved = JSON.parse(localStorage.getItem(SESSION_KEY) || "null");
      if (saved?.profile) {
        state.profile = saved.profile;
        state.onStatus({ signedIn: false, profile: saved.profile, message: "Drive session expired. Sign in to sync." });
      }
    } catch {}
  }

  window.UploadPilotDrive = {
    init,
    signIn,
    signOut,
    saveNow,
    loadFromDrive,
    scheduleSave,
    fetchRestoreInfo,
    restore,
  };
})();
