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

function markdownSummaryHtml(markdown) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let listOpen = false;
  const closeList = () => {
    if (listOpen) {
      html.push("</ul>");
      listOpen = false;
    }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      closeList();
      continue;
    }
    if (line.startsWith("## ")) {
      closeList();
      html.push(`<h3>${escapeHtml(line.slice(3))}</h3>`);
    } else if (line.startsWith("# ")) {
      closeList();
      html.push(`<h2>${escapeHtml(line.slice(2))}</h2>`);
    } else if (line.startsWith("- ")) {
      if (!listOpen) {
        html.push("<ul>");
        listOpen = true;
      }
      html.push(`<li>${formatInline(line.slice(2))}</li>`);
    } else {
      closeList();
      html.push(`<p>${formatInline(line)}</p>`);
    }
  }
  closeList();
  return html.join("");
}

function formatInline(text) {
  return escapeHtml(text)
    .replace(/\b(\d{1,2}:\d{2})\b/g, '<span class="time-chip">$1</span>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function localServerError(err) {
  if (!USE_LOCAL_API) return err.message;
  return `${err.message}\n\nOpen the local UploadPilot server first: ${LOCAL_API_BASE}`;
}

function renderResult(data) {
  lastResult = data || {};
  const download = $("#download-md");
  if (data.cache?.download_url) {
    download.href = apiUrl(data.cache.download_url);
    download.download = data.cache.filename || "";
    download.textContent = data.cache.format === "json" ? "Download JSON" : "Download Markdown";
    download.classList.remove("hidden");
  } else {
    download.removeAttribute("href");
    download.classList.add("hidden");
  }
  const results = $("#scrape-results");
  const items = data.items || (data.item ? [data.item] : []);
  if (!items.length) {
    results.innerHTML = data.error ? `<div class="summary-error">${escapeHtml(data.error)}</div>` : "";
    return;
  }
  results.innerHTML = items.map((item, index) => `
    <article class="summary-card">
      <div class="summary-card-head">
        <strong>${escapeHtml(item.title || `Video ${index + 1}`)}</strong>
        ${item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">Open</a>` : ""}
      </div>
      ${item.error ? `<div class="summary-error">${escapeHtml(item.error)}</div>` : ""}
      <div class="markdown-summary">${markdownSummaryHtml(item.summary || "")}</div>
    </article>
  `).join("");
}

function renderCache(items) {
  const cacheList = $("#cache-list");
  if (!items.length) {
    cacheList.innerHTML = `<div class="muted">No cached summaries yet.</div>`;
    return;
  }
  cacheList.innerHTML = items.map((entry) => `
    <article class="cache-entry">
      <div>
        <strong>${escapeHtml(entry.title || "Video summary")}</strong>
        <p>${escapeHtml(entry.title || "Video summary")}: ${escapeHtml(entry.excerpt || "")}</p>
        <small>${escapeHtml(entry.created_at || "")} · ${escapeHtml(entry.count || 1)} item(s)</small>
      </div>
      <a class="button-link" href="${escapeHtml(apiUrl(entry.download_url || ""))}" download="${escapeHtml(entry.filename || "")}">${entry.format === "json" ? "Download JSON" : "Download"}</a>
    </article>
  `).join("");
}

async function loadCache() {
  try {
    const response = await fetch(apiUrl("/api/scrape/cache"), { cache: "no-store" });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    renderCache(data.items || []);
  } catch (err) {
    $("#cache-list").innerHTML = `<div class="summary-error">${escapeHtml(localServerError(err))}</div>`;
  }
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
    await loadCache();
    setStatus(`${data.count || 1} extracted`);
  } catch (err) {
    renderResult({ ok: false, error: localServerError(err) });
    setStatus("failed", true);
  } finally {
    $("#scrape-run").disabled = false;
  }
};

$("#refresh-cache").onclick = loadCache;
loadCache();
