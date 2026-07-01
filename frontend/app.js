// app.js — talks to the vectorize backend API.
//
// Configure API_BASE_URL for your deployment:
//   - local dev:  http://localhost:8000
//   - production: wherever backend/ is hosted (Render, Fly.io, Railway, a VPS, ...)
// GitHub Pages only serves this static frontend; the API must be reachable
// over HTTPS from wherever this page is served.
const API_BASE_URL = window.VECTORIZE_API_BASE_URL || "http://localhost:8000";

const POLL_INTERVAL_MS = 500;

const el = (id) => document.getElementById(id);

const dropzone = el("dropzone");
const fileInput = el("fileInput");
const landing = el("landing");
const workspace = el("workspace");
const fileNameEl = el("fileName");
const swapFileBtn = el("swapFile");

const detailInput = el("detail");
const detailValue = el("detailValue");
const colorsInput = el("colors");
const colorsValue = el("colorsValue");
const seamFixInput = el("seamFix");
const preserveTransparencyInput = el("preserveTransparency");

const vectorizeBtn = el("vectorizeBtn");
const cancelBtn = el("cancelBtn");
const serverNote = el("serverNote");

const progressBlock = el("progressBlock");
const stageText = el("stageText");
const pctText = el("pctText");
const progressFill = el("progressFill");
const etaText = el("etaText");

const resultBlock = el("resultBlock");
const downloadBtn = el("downloadBtn");
const errorText = el("errorText");

let selectedFile = null;
let activeJobId = null;
let pollHandle = null;
let downloadObjectUrl = null;

// ---------------------------------------------------------------- helpers

function fmtSeconds(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return `${m}m ${String(rem).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

function resetOutputPanel() {
  progressBlock.hidden = true;
  resultBlock.hidden = true;
  errorText.hidden = true;
  errorText.textContent = "";
  serverNote.hidden = true;
  serverNote.textContent = "";
  stageText.textContent = "starting";
  pctText.textContent = "0%";
  progressFill.style.width = "0%";
  etaText.textContent = "estimated time left: —";
  vectorizeBtn.disabled = false;
  vectorizeBtn.textContent = "Vectorize";
}

function showWorkspace(file) {
  selectedFile = file;
  landing.hidden = true;
  workspace.hidden = false;
  fileNameEl.textContent = file.name;
  resetOutputPanel();
}

function acceptFiles(fileList) {
  const files = Array.from(fileList || []);
  const image = files.find((f) => /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(f.name));
  if (image) showWorkspace(image);
}

// ---------------------------------------------------------------- file selection

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});

fileInput.addEventListener("change", () => acceptFiles(fileInput.files));

["dragenter", "dragover"].forEach((evt) => {
  window.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("is-dragover");
  });
});

["dragleave", "drop"].forEach((evt) => {
  window.addEventListener(evt, (e) => {
    if (evt === "drop") e.preventDefault();
    dropzone.classList.remove("is-dragover");
  });
});

window.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
    acceptFiles(e.dataTransfer.files);
  }
});

swapFileBtn.addEventListener("click", () => {
  landing.hidden = false;
  workspace.hidden = true;
  fileInput.value = "";
  stopPolling();
});

// ---------------------------------------------------------------- settings UI

detailInput.addEventListener("input", () => {
  detailValue.textContent = detailInput.value;
});
colorsInput.addEventListener("input", () => {
  colorsValue.textContent = colorsInput.value;
});

// ---------------------------------------------------------------- job lifecycle

vectorizeBtn.addEventListener("click", startJob);
cancelBtn.addEventListener("click", cancelJob);

async function startJob() {
  if (!selectedFile) return;

  resetOutputPanel();
  progressBlock.hidden = false;
  vectorizeBtn.disabled = true;
  vectorizeBtn.textContent = "Vectorizing…";

  const form = new FormData();
  form.append("file", selectedFile);
  form.append("detail", detailInput.value);
  form.append("colors", colorsInput.value);
  form.append("seam_fix", seamFixInput.checked);
  form.append("preserve_transparency", preserveTransparencyInput.checked);

  try {
    const res = await fetch(`${API_BASE_URL}/api/jobs`, { method: "POST", body: form });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `server responded ${res.status}`);
    }
    const data = await res.json();
    activeJobId = data.job_id;
    pollHandle = setInterval(pollJob, POLL_INTERVAL_MS);
  } catch (err) {
    handleFailure(err.message || "could not reach the vectorize backend");
  }
}

async function cancelJob() {
  if (!activeJobId) return;
  cancelBtn.disabled = true;
  stageText.textContent = "cancelling";
  try {
    await fetch(`${API_BASE_URL}/api/jobs/${activeJobId}/cancel`, { method: "POST" });
  } catch (err) {
    // polling will still pick up whatever the server's real state ends up being
  }
}

async function pollJob() {
  if (!activeJobId) return;

  try {
    const res = await fetch(`${API_BASE_URL}/api/jobs/${activeJobId}`);
    if (!res.ok) throw new Error(`server responded ${res.status}`);
    const data = await res.json();

    const pct = Math.round((data.progress || 0) * 100);
    stageText.textContent = data.stage || data.state;
    pctText.textContent = `${pct}%`;
    progressFill.style.width = `${pct}%`;
    etaText.textContent = `estimated time left: ${fmtSeconds(data.eta_seconds)}`;

    if (data.state === "done") {
      stopPolling();
      await finishJob();
    } else if (data.state === "cancelled") {
      stopPolling();
      progressBlock.hidden = true;
      vectorizeBtn.disabled = false;
      vectorizeBtn.textContent = "Vectorize";
    } else if (data.state === "error") {
      stopPolling();
      handleFailure(data.error || "vectorization failed");
    }
  } catch (err) {
    stopPolling();
    handleFailure(err.message || "lost connection to the vectorize backend");
  }
}

async function finishJob() {
  try {
    const res = await fetch(`${API_BASE_URL}/api/jobs/${activeJobId}/download`);
    if (!res.ok) throw new Error(`server responded ${res.status}`);
    const blob = await res.blob();

    if (downloadObjectUrl) URL.revokeObjectURL(downloadObjectUrl);
    downloadObjectUrl = URL.createObjectURL(blob);

    const stem = selectedFile.name.includes(".")
      ? selectedFile.name.slice(0, selectedFile.name.lastIndexOf("."))
      : selectedFile.name;

    downloadBtn.href = downloadObjectUrl;
    downloadBtn.download = `${stem}_vectorized.svg`;

    progressBlock.hidden = true;
    resultBlock.hidden = false;
    vectorizeBtn.disabled = false;
    vectorizeBtn.textContent = "Vectorize again";
  } catch (err) {
    handleFailure(err.message || "could not download the finished SVG");
  }
}

function handleFailure(message) {
  stopPolling();
  progressBlock.hidden = true;
  vectorizeBtn.disabled = false;
  vectorizeBtn.textContent = "Vectorize";
  errorText.hidden = false;
  errorText.textContent = message;

  if (message.includes("reach") || message.includes("connection")) {
    serverNote.hidden = false;
    serverNote.textContent = `Can't reach the API at ${API_BASE_URL}. Is the backend running?`;
  }
}

function stopPolling() {
  if (pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
  }
}
