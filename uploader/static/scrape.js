const $ = (s) => document.querySelector(s);
const ls = window.localStorage;
const LOCAL_API_BASE = "http://127.0.0.1:3000";
const USE_LOCAL_API = !["127.0.0.1", "localhost"].includes(window.location.hostname);
const API_BASE = USE_LOCAL_API ? LOCAL_API_BASE : "";
let lastResult = {};

function apiUrl(path) {
  return `${API_BASE}${path}`;
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"]/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
  }[c]));
}

function setStatus(text, isError = false) {
  const status = $("#scrape-status");
  status.textContent = text;
  status.classList.toggle("error", isError);
}

function localServerError(err) {
  if (!USE_LOCAL_API) return err.message;
  return `${err.message}\n\nOpen the local UploadPilot server first: ${LOCAL_API_BASE}`;
}

function renderResult(data) {
  lastResult = data || {};
  $("#scrape-json").textContent = JSON.stringify(lastResult, null, 2);
  const results = $("#scrape-results");
  const items = data.items || (data.item ? [data.item] : []);
  if (!items.length) {
    results.innerHTML = "";
    return;
  }
  results.innerHTML = items.map((item, index) => `
    <article class="summary-card">
      <div class="summary-card-head">
        <strong>${escapeHtml(item.title || `Video ${index + 1}`)}</strong>
        ${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">Open</a>` : ""}
      </div>
      ${item.error ? `<div class="summary-error">${escapeHtml(item.error)}</div>` : ""}
      <pre>${escapeHtml(item.summary || "")}</pre>
    </article>
  `).join("");
}

$("#scrape-url").value = ls.getItem("scrape_url") || "";
$("#scrape-scope").value = ls.getItem("scrape_scope") || "auto";
$("#scrape-max").value = ls.getItem("scrape_max") || "0";

$("#scrape-run").onclick = async () => {
  const url = $("#scrape-url").value.trim();
  const scope = $("#scrape-scope").value;
  const max_videos = Number($("#scrape-max").value || 0);
  ls.setItem("scrape_url", url);
  ls.setItem("scrape_scope", scope);
  ls.setItem("scrape_max", String(max_videos));
  if (!url) {
    setStatus("URL required", true);
    return;
  }
  setStatus("extracting...");
  $("#scrape-run").disabled = true;
  try {
    const response = await fetch(apiUrl("/api/scrape/summary"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ url, scope, max_videos }),
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : {};
    if (!response.ok) {
      throw new Error(data.detail || text || response.statusText);
    }
    renderResult(data);
    setStatus(`${data.count || 1} extracted`);
  } catch (err) {
    renderResult({ ok: false, error: localServerError(err) });
    setStatus("failed", true);
  } finally {
    $("#scrape-run").disabled = false;
  }
};

$("#copy-json").onclick = async () => {
  const text = JSON.stringify(lastResult, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    setStatus("copied");
  } catch {
    $("#scrape-json").focus();
    setStatus("copy blocked", true);
  }
};
