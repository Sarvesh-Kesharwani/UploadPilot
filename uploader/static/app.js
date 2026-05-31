const $ = (s) => document.querySelector(s);
const tbody = $("#items tbody");
const summary = $("#summary");
const startBtn = $("#start");
const setupCard = document.querySelector(".form-card");
const driveStatus = $("#drive-status");
const openSpaceBtn = $("#open-space");
const pickFolderBtn = $("#pick-folder");
const restoreBtn = $("#restore-yesterday");
const googleSigninBtn = $("#google-signin");
const googleSignoutBtn = $("#google-signout");
const clearLastBatchBtn = $("#clear-last-batch");
const LAST_BATCH_KEY = "uploadpilot_last_batch";
const ls = window.localStorage;
let lastBatchSerialized = ls.getItem(LAST_BATCH_KEY) || "";
let historyCache = null;
const LOCAL_API_BASE = "http://127.0.0.1:3000";
const USE_LOCAL_API = !["127.0.0.1", "localhost"].includes(window.location.hostname);
const API_BASE = USE_LOCAL_API ? LOCAL_API_BASE : "";

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

function localServerError(err) {
  if (!USE_LOCAL_API) return err.message;
  return `${err.message}\n\nOpen the local UploadPilot server first: ${LOCAL_API_BASE}\nThe hosted Vercel page cannot read folders from your D: drive directly.`;
}

const statusLabels = {
  queued: "in queue",
  uploading: "uploading",
  settling: "settling",
  renaming: "renaming",
  validating: "validating",
  uploaded: "Uploaded",
  failed: "failed",
  skipped: "skipped",
};

function renderState(s) {
  const job = s.job;
  if (!job) {
    renderLastBatch();
    startBtn.disabled = false;
    startBtn.textContent = "Start upload";
    return;
  }

  persistLastBatch(job);
  const counts = job.items.reduce((a, it) => {
    a[it.status] = (a[it.status] || 0) + 1;
    return a;
  }, {});
  const total = job.items.length;
  const uploaded = counts.uploaded || 0;

  summary.textContent = `job ${job.id} - ${job.mode || "sequential"} - ${uploaded}/${total} Uploaded`
    + (job.finished_at ? " - finished" : " - running")
    + (job.cancelled ? " - CANCELLING" : "");
  startBtn.disabled = !job.finished_at;
  startBtn.textContent = job.finished_at ? "Start upload" : "Uploading...";

  renderBatchRows(job, !job.finished_at);
}

function renderBatchRows(job, isLiveJob = false) {
  tbody.innerHTML = job.items.map((it, i) => `
    <tr>
      <td>${i + 1}</td>
      <td class="path-cell">${escape(it.path || it.name)}</td>
      <td class="title-cell">${escape(targetTitle(it))}</td>
      <td class="title-cell">${escape(youlearnAiName(it))}</td>
      <td class="title-cell">${escape(currentTitle(it))}</td>
      <td>${progressBar(it)}</td>
      <td><span class="badge ${it.status}">${statusLabels[it.status] || it.status}</span></td>
      <td class="muted">${escape(it.error || "")}</td>
      <td>${retryButton(job, it, i)}</td>
      <td>${retryNamingButton(it, i)}</td>
      <td>${retryUploadButton(it, i, isLiveJob)}</td>
    </tr>`).join("");
  document.querySelectorAll("[data-retry-rename]").forEach((button) => {
    button.addEventListener("click", () => retryRename(Number(button.dataset.retryRename)));
  });
  document.querySelectorAll("[data-retry-upload]").forEach((button) => {
    button.addEventListener("click", () => retryUpload(Number(button.dataset.retryUpload)));
  });
}

function retryButton(job, item, index) {
  if (!canContinueRename(item)) return "";
  return `<button class="retry-btn" data-retry-rename="${index}">Continue rename</button>`;
}

function retryNamingButton(item, index) {
  if (!canRetryNaming(item)) return "";
  return `<button class="retry-btn" data-retry-rename="${index}">Retry naming</button>`;
}

function retryUploadButton(item, index, isLiveJob) {
  if (!canRetryUpload(item, isLiveJob)) return "";
  return `<button class="retry-btn" data-retry-upload="${index}">Retry upload</button>`;
}

function targetTitle(item) {
  return item.target_title || item.name || "";
}

function currentTitle(item) {
  return item.current_title || item.display_title || "";
}

function youlearnAiName(item) {
  const stored = item.youlearn_ai_name || "";
  if (stored) return stored;
  const current = currentTitle(item);
  const target = targetTitle(item);
  if (!current || !target) return "";
  return current.trim().toLowerCase() === target.trim().toLowerCase() ? "" : current;
}

function progressInfo(item) {
  const statusProgress = {
    queued: 0,
    uploading: 25,
    settling: 55,
    renaming: 75,
    validating: 90,
    uploaded: 100,
    skipped: 100,
    failed: Number(item.progress_percent || 0),
  };
  const raw = Number(item.progress_percent ?? statusProgress[item.status] ?? 0);
  const percent = Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 0));
  const label = item.status === "failed"
    ? `${percent}% failed`
    : `${percent}% ${statusLabels[item.status] || item.status || "pending"}`;
  return { percent, label };
}

function progressBar(item) {
  const { percent, label } = progressInfo(item);
  const active = ["uploading", "settling", "renaming", "validating"].includes(item.status) ? " active" : "";
  const failed = item.status === "failed" ? " failed" : "";
  return `<div class="progress-cell" title="${escape(label)}">
    <div class="video-progress${active}${failed}" aria-label="${escape(label)}">
      <span style="width:${percent}%"></span>
    </div>
    <small>${escape(label)}</small>
  </div>`;
}

function canContinueRename(item) {
  const target = targetTitle(item).trim().toLowerCase();
  const current = currentTitle(item).trim().toLowerCase();
  if (!target) return false;
  if (current && current === target) return false;
  if (["queued", "uploading", "uploaded", "skipped"].includes(item.status)) return false;
  return Boolean(current || item.content_url);
}

function canRetryNaming(item) {
  if (!targetTitle(item).trim()) return false;
  if (["queued", "uploading"].includes(item.status)) return false;
  return Boolean(item.content_url || currentTitle(item) || youlearnAiName(item));
}

function canRetryUpload(item, isLiveJob) {
  if (!targetTitle(item).trim()) return false;
  if (!item.path) return false;
  if (isLiveJob && ["queued", "uploading", "settling", "renaming", "validating"].includes(item.status)) return false;
  return true;
}

function persistLastBatch(job) {
  const snapshot = {
    id: job.id,
    space_url: job.space_url,
    folder: job.folder,
    mode: job.mode || "sequential",
    started_at: job.started_at,
    finished_at: job.finished_at,
    cancelled: job.cancelled,
    savedAt: Date.now(),
    items: job.items || [],
  };
  const serialized = JSON.stringify(snapshot);
  if (serialized !== lastBatchSerialized) {
    lastBatchSerialized = serialized;
    ls.setItem(LAST_BATCH_KEY, serialized);
    window.UploadPilotDrive?.scheduleSave();
  }
  clearLastBatchBtn.disabled = false;
}

function getLastBatch() {
  try {
    return JSON.parse(ls.getItem(LAST_BATCH_KEY) || "null");
  } catch {
    return null;
  }
}

function setLastBatch(batch) {
  if (!batch || !Array.isArray(batch.items)) return;
  lastBatchSerialized = JSON.stringify(batch);
  ls.setItem(LAST_BATCH_KEY, lastBatchSerialized);
  renderLastBatch();
}

function renderLastBatch() {
  const batch = getLastBatch() || historyCache?.last_batch;
  if (!batch) {
    summary.textContent = "idle";
    tbody.innerHTML = "";
    clearLastBatchBtn.disabled = true;
    return;
  }
  const uploaded = batch.items.filter((it) => it.status === "uploaded").length;
  summary.textContent = `last batch ${batch.id || ""} - ${batch.mode || "sequential"} - ${uploaded}/${batch.items.length} Uploaded`;
  clearLastBatchBtn.disabled = false;
  renderBatchRows(batch);
}

function escape(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
  }[c]));
}

const es = new EventSource(apiUrl("/api/events"));
es.onmessage = (e) => {
  try {
    renderState(JSON.parse(e.data));
  } catch {}
};
es.onerror = () => {
  // Browser reconnects automatically.
};

async function refreshState() {
  try {
    const r = await fetch(apiUrl("/api/state"), { cache: "no-store" });
    if (r.ok) renderState(await r.json());
  } catch {}
}
setInterval(refreshState, 2000);
refreshHistory().then(refreshState);

async function refreshHistory() {
  try {
    const r = await fetch(apiUrl("/api/history"), { cache: "no-store" });
    if (r.ok) {
      historyCache = await r.json();
      renderLastBatch();
    }
  } catch {}
}

async function importHistory(data) {
  const r = await fetch(apiUrl("/api/history/import"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ data }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

$("#space").value = ls.getItem("space") || "";
$("#folder").value = ls.getItem("folder") || "";
const savedMode = ls.getItem("upload_mode") || "sequential";
const modeInput = document.querySelector(`input[name="mode"][value="${savedMode}"]`);
if (modeInput) modeInput.checked = true;

function getAppData() {
  return {
    space: $("#space").value.trim(),
    folder: $("#folder").value.trim(),
    upload_mode: document.querySelector('input[name="mode"]:checked')?.value || "sequential",
    last_batch: getLastBatch(),
    upload_history: historyCache,
  };
}

function applyAppData(data) {
  if (!data || typeof data !== "object") return;
  if (typeof data.space === "string") {
    $("#space").value = data.space;
    ls.setItem("space", data.space);
  }
  if (typeof data.folder === "string") {
    $("#folder").value = data.folder;
    ls.setItem("folder", data.folder);
  }
  if (typeof data.upload_mode === "string") {
    const input = document.querySelector(`input[name="mode"][value="${data.upload_mode}"]`);
    if (input) {
      input.checked = true;
      ls.setItem("upload_mode", data.upload_mode);
    }
  }
  if (Object.prototype.hasOwnProperty.call(data, "last_batch")) {
    if (data.last_batch) {
      setLastBatch(data.last_batch);
    } else {
      ls.removeItem(LAST_BATCH_KEY);
      lastBatchSerialized = "";
      renderLastBatch();
    }
  }
  if (data.upload_history) {
    historyCache = data.upload_history;
    importHistory(data).then((merged) => {
      historyCache = merged;
      renderLastBatch();
    }).catch(() => {});
  }
}

function setEditBlocked(blocked) {
  setupCard.classList.toggle("edit-blocked", blocked);
  $("#space").disabled = blocked;
  $("#folder").disabled = blocked;
  $("#preview").disabled = blocked;
  startBtn.disabled = blocked || startBtn.textContent === "Uploading...";
  document.querySelectorAll('input[name="mode"]').forEach((input) => { input.disabled = blocked; });
  pickFolderBtn.disabled = blocked;
}

function setDriveStatus(info) {
  const signedIn = Boolean(info.signedIn);
  driveStatus.classList.toggle("hidden", true);
  driveStatus.textContent = "";
  driveStatus.classList.toggle("error", false);
  googleSigninBtn.classList.toggle("hidden", signedIn);
  googleSignoutBtn.classList.toggle("hidden", !signedIn);
  restoreBtn.disabled = !signedIn;
  restoreBtn.title = signedIn ? "Check and restore yesterday's Drive backup" : "Sign in with Google Drive first";
  if (info.error) {
    restoreBtn.title = info.error;
    return;
  }
  const name = info.profile?.name || info.profile?.email || "";
  googleSignoutBtn.title = signedIn && name ? `Signed in as ${name}` : "";
  if (signedIn) refreshRestoreInfo();
}

async function initDriveSync() {
  const config = await fetch(apiUrl("/api/config"), { cache: "no-store" }).then((r) => r.json());
  config.apiBase = API_BASE;
  window.UploadPilotDrive.init({
    config,
    getData: getAppData,
    applyData: applyAppData,
    setEditBlocked,
    onStatus: setDriveStatus,
  });
}

function saveLocalForm() {
  const data = getAppData();
  ls.setItem("space", data.space);
  ls.setItem("folder", data.folder);
  ls.setItem("upload_mode", data.upload_mode);
  window.UploadPilotDrive?.scheduleSave();
}

["space", "folder"].forEach((id) => {
  $(`#${id}`).addEventListener("input", saveLocalForm);
});
document.querySelectorAll('input[name="mode"]').forEach((input) => {
  input.addEventListener("change", saveLocalForm);
});

$("#preview").onclick = async () => {
  const folder = $("#folder").value.trim();
  saveLocalForm();
  const out = $("#preview-out");
  out.textContent = "scanning...";
  let r;
  try {
    r = await fetch(apiUrl("/api/preview"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ folder }),
    });
  } catch (err) {
    out.textContent = "error: " + localServerError(err);
    return;
  }
  if (!r.ok) {
    out.textContent = "error: " + await r.text();
    return;
  }
  const j = await r.json();
  out.innerHTML = `<strong>${j.count}</strong> file(s):<br>`
    + j.files.map(escape).map((n) => `- ${n}`).join("<br>");
};

startBtn.onclick = async () => {
  const space_url = $("#space").value.trim();
  const folder = $("#folder").value.trim();
  const mode = document.querySelector('input[name="mode"]:checked')?.value || "sequential";
  saveLocalForm();
  $("#preview-out").innerHTML = "";
  startBtn.disabled = true;
  startBtn.textContent = "Uploading...";
  let r;
  try {
    r = await fetch(apiUrl("/api/start"), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ space_url, folder, mode }),
    });
  } catch (err) {
    startBtn.disabled = false;
    startBtn.textContent = "Start upload";
    alert("start failed: " + localServerError(err));
    return;
  }
  if (!r.ok) {
    startBtn.disabled = false;
    startBtn.textContent = "Start upload";
    alert("start failed: " + await r.text());
    return;
  }
  const j = await r.json();
  renderState(j.state);
};

$("#cancel").onclick = async () => {
  await fetch(apiUrl("/api/cancel"), { method: "POST" });
};

clearLastBatchBtn.onclick = () => {
  if (!confirm("Clear the saved last batch from this app and Drive sync data?")) return;
  ls.removeItem(LAST_BATCH_KEY);
  lastBatchSerialized = "";
  if (historyCache) historyCache.last_batch = null;
  renderLastBatch();
  fetch(apiUrl("/api/history/clear-last-batch"), { method: "POST" })
    .then((r) => r.ok ? r.json() : null)
    .then((data) => { if (data) historyCache = data; })
    .catch(() => {});
  window.UploadPilotDrive?.scheduleSave();
};

async function retryRename(index) {
  const batch = getLastBatch() || historyCache?.last_batch;
  const item = batch?.items?.[index];
  if (!batch || !item) return;
  try {
    item.status = "renaming";
    item.progress_percent = 75;
    item.error = "";
    item.rename_started_at = new Date().toISOString();
    setLastBatch(batch);
    const r = await fetch(apiUrl("/api/rename"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        space_url: batch.space_url,
        display_title: currentTitle(item),
        target_title: targetTitle(item),
        youlearn_ai_name: youlearnAiName(item),
        content_url: item.content_url || "",
        record_id: item.record_id || `${batch.id || ""}:${index}`,
        job_id: batch.id || "",
        item_index: index,
        source_path: item.path || "",
        folder: batch.folder || "",
        mode: batch.mode || "",
      }),
    });
    const result = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(result.error || JSON.stringify(result) || r.statusText);
    item.status = "uploaded";
    item.progress_percent = 100;
    item.resume_checked_title = result.current_title || currentTitle(item);
    item.youlearn_ai_name = result.youlearn_ai_name || item.youlearn_ai_name || youlearnAiName(item);
    item.display_title = targetTitle(item);
    item.current_title = targetTitle(item);
    item.renamed_at = new Date().toISOString();
    item.validated_at = item.renamed_at;
    item.error = "";
  } catch (err) {
    item.status = "failed";
    item.error = err.message;
  }
  batch.finished_at = Date.now() / 1000;
  setLastBatch(batch);
  await refreshHistory();
  window.UploadPilotDrive?.scheduleSave();
}

async function retryUpload(index) {
  const batch = getLastBatch() || historyCache?.last_batch;
  const item = batch?.items?.[index];
  if (!batch || !item) return;
  try {
    item.status = "uploading";
    item.progress_percent = 25;
    item.error = "";
    item.upload_started_at = new Date().toISOString();
    setLastBatch(batch);
    const r = await fetch(apiUrl("/api/reupload"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        space_url: batch.space_url,
        source_path: item.path || "",
        target_title: targetTitle(item),
        display_title: currentTitle(item),
        youlearn_ai_name: youlearnAiName(item),
        content_url: item.content_url || "",
        record_id: item.record_id || `${batch.id || ""}:${index}`,
        job_id: batch.id || "",
        item_index: index,
        folder: batch.folder || "",
        mode: batch.mode || "",
      }),
    });
    const result = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(result.error || JSON.stringify(result) || r.statusText);
    const doneAt = new Date().toISOString();
    item.status = "uploaded";
    item.progress_percent = 100;
    item.resume_checked_title = result.current_title || currentTitle(item);
    item.youlearn_ai_name = result.youlearn_ai_name || item.youlearn_ai_name || youlearnAiName(item);
    item.content_url = result.content_url || item.content_url || "";
    item.display_title = targetTitle(item);
    item.current_title = targetTitle(item);
    item.uploaded_at = doneAt;
    item.renamed_at = doneAt;
    item.validated_at = doneAt;
    item.error = "";
  } catch (err) {
    item.status = "failed";
    item.error = err.message;
  }
  batch.finished_at = Date.now() / 1000;
  setLastBatch(batch);
  await refreshHistory();
  window.UploadPilotDrive?.scheduleSave();
}

googleSigninBtn.onclick = () => window.UploadPilotDrive.signIn();
googleSignoutBtn.onclick = () => window.UploadPilotDrive.signOut();
restoreBtn.disabled = true;
restoreBtn.onclick = async () => {
  if (!confirm("Restore yesterday's Drive backup? Current Drive state will be snapshotted first.")) return;
  try {
    const result = await window.UploadPilotDrive.restore();
    const counts = result.counts ? ` ${JSON.stringify(result.counts)}` : "";
    alert(`Restored ${result.date}.${counts}`);
    location.reload();
  } catch (err) {
    alert(err.message);
  }
};

async function refreshRestoreInfo() {
  try {
    const info = await window.UploadPilotDrive.fetchRestoreInfo();
    restoreBtn.title = info.hasYesterdayBackup
      ? `Yesterday backup available (${info.defaultDate}).`
      : `No yesterday backup found (${info.defaultDate}).`;
  } catch (err) {
    restoreBtn.title = err.message;
  }
}

initDriveSync().catch((err) => {
  driveStatus.textContent = err.message;
});
openSpaceBtn.onclick = () => {
  const url = $("#space").value.trim();
  if (url) window.open(url, "_blank", "noreferrer");
};

openSpaceBtn.href = "#";

pickFolderBtn.onclick = async () => {
  saveLocalForm();
  const current = $("#folder").value.trim();
  let r;
  try {
    r = await fetch(apiUrl("/api/folder/pick"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ folder: current }),
    });
  } catch (err) {
    alert("Folder picker failed: " + localServerError(err));
    return;
  }
  if (!r.ok) {
    alert("Folder picker failed: " + await r.text());
    return;
  }
  const data = await r.json();
  if (data.folder) {
    $("#folder").value = data.folder;
    ls.setItem("folder", data.folder);
    window.UploadPilotDrive?.scheduleSave();
  }
};

["space"].forEach((id) => {
  $(`#${id}`).addEventListener("input", () => {
    saveLocalForm();
    const url = $("#space").value.trim();
    openSpaceBtn.href = url || "#";
  });
});

