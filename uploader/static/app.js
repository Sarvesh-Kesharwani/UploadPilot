const $ = (s) => document.querySelector(s);
const tbody = $("#items tbody");
const summary = $("#summary");
const startBtn = $("#start");
const setupCard = document.querySelector(".form-card");
const driveStatus = $("#drive-status");
const restoreStatus = $("#restore-status");
const clearLastBatchBtn = $("#clear-last-batch");
const LAST_BATCH_KEY = "uploadpilot_last_batch";
const ls = window.localStorage;
let lastBatchSerialized = ls.getItem(LAST_BATCH_KEY) || "";

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

  renderBatchRows(job);
}

function renderBatchRows(job) {
  tbody.innerHTML = job.items.map((it, i) => `
    <tr>
      <td>${i + 1}</td>
      <td class="path-cell">${escape(it.path || it.name)}</td>
      <td>${escape(it.display_title || "")}</td>
      <td><span class="badge ${it.status}">${statusLabels[it.status] || it.status}</span></td>
      <td class="muted">${escape(it.error || "")}</td>
      <td>${retryButton(job, it, i)}</td>
    </tr>`).join("");
  document.querySelectorAll("[data-retry-rename]").forEach((button) => {
    button.addEventListener("click", () => retryRename(Number(button.dataset.retryRename)));
  });
}

function retryButton(job, item, index) {
  if (item.status !== "failed" || !item.display_title) return "";
  const title = item.name || "";
  if ((item.display_title || "").trim().toLowerCase() === title.trim().toLowerCase()) return "";
  return `<button class="retry-btn" data-retry-rename="${index}">Continue rename</button>`;
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
  const batch = getLastBatch();
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

const es = new EventSource("/api/events");
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
    const r = await fetch("/api/state", { cache: "no-store" });
    if (r.ok) renderState(await r.json());
  } catch {}
}
setInterval(refreshState, 2000);
refreshState();

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
}

function setEditBlocked(blocked) {
  setupCard.classList.toggle("edit-blocked", blocked);
  $("#space").disabled = blocked;
  $("#folder").disabled = blocked;
  $("#preview").disabled = blocked;
  startBtn.disabled = blocked || startBtn.textContent === "Uploading...";
  document.querySelectorAll('input[name="mode"]').forEach((input) => { input.disabled = blocked; });
}

function setDriveStatus(info) {
  if (info.error) {
    driveStatus.textContent = info.error;
    driveStatus.classList.add("error");
    return;
  }
  driveStatus.classList.remove("error");
  const name = info.profile?.name || info.profile?.email || "";
  driveStatus.textContent = info.signedIn ? `Drive signed in${name ? `: ${name}` : ""}` : (info.message || "Drive signed out");
  if (info.signedIn) refreshRestoreInfo();
}

async function initDriveSync() {
  const config = await fetch("/api/config", { cache: "no-store" }).then((r) => r.json());
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
  const r = await fetch("/api/preview", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ folder }),
  });
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
  const r = await fetch("/api/start", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ space_url, folder, mode }),
  });
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
  await fetch("/api/cancel", { method: "POST" });
};

clearLastBatchBtn.onclick = () => {
  if (!confirm("Clear the saved last batch from this app and Drive sync data?")) return;
  ls.removeItem(LAST_BATCH_KEY);
  lastBatchSerialized = "";
  renderLastBatch();
  window.UploadPilotDrive?.scheduleSave();
};

async function retryRename(index) {
  const batch = getLastBatch();
  const item = batch?.items?.[index];
  if (!batch || !item) return;
  try {
    item.status = "renaming";
    item.error = "";
    setLastBatch(batch);
    const r = await fetch("/api/rename", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        space_url: batch.space_url,
        display_title: item.display_title,
        target_title: item.name,
        content_url: item.content_url || "",
      }),
    });
    if (!r.ok) throw new Error(await r.text());
    item.status = "uploaded";
    item.display_title = item.name;
    item.error = "";
  } catch (err) {
    item.status = "failed";
    item.error = err.message;
  }
  batch.finished_at = Date.now() / 1000;
  setLastBatch(batch);
  window.UploadPilotDrive?.scheduleSave();
}

$("#google-signin").onclick = () => window.UploadPilotDrive.signIn();
$("#google-signout").onclick = () => window.UploadPilotDrive.signOut();
$("#restore-yesterday").onclick = async () => {
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
    restoreStatus.textContent = info.hasYesterdayBackup
      ? `Yesterday backup available (${info.defaultDate}).`
      : `No yesterday backup found (${info.defaultDate}).`;
  } catch (err) {
    restoreStatus.textContent = err.message;
  }
}

initDriveSync().catch((err) => {
  driveStatus.textContent = err.message;
});
