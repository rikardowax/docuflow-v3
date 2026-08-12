const API = "/v2";
const DEMO = { client_id: "demo_client", client_secret: "demo_secret" };

let token = sessionStorage.getItem("df_token");

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 4000);
}

async function login() {
  const res = await fetch(`${API}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...DEMO, grant_type: "client_credentials" }),
  });
  if (!res.ok) throw new Error("Authentification échouée");
  const data = await res.json();
  token = data.access_token;
  sessionStorage.setItem("df_token", token);
  return token;
}

async function api(path, options = {}) {
  if (!token) await login();
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  let res = await fetch(`${API}${path}`, { ...options, headers });
  if (res.status === 401) {
    await login();
    headers.Authorization = `Bearer ${token}`;
    res = await fetch(`${API}${path}`, { ...options, headers });
  }
  return res;
}

function setStatus(ok, text) {
  $("#statusDot").className = `status-dot ${ok ? "ok" : "err"}`;
  $("#statusText").textContent = text;
}

function renderFields(container, fields) {
  const entries = Object.entries(fields || {}).filter(([, v]) => v != null && v !== "");
  if (!entries.length) {
    container.innerHTML = "<p>Aucun champ extrait.</p>";
    return;
  }
  container.innerHTML = `<div class="field-grid">${entries
    .map(
      ([k, v]) =>
        `<div class="field-item"><label>${k.replace(/_/g, " ")}</label><span class="val">${escapeHtml(String(v))}</span></div>`
    )
    .join("")}</div>`;
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function setupDropzone(zoneId, inputId, previewId, onFile) {
  const zone = $(zoneId);
  const input = $(inputId);
  const preview = previewId ? $(previewId) : null;

  zone.addEventListener("click", () => input.click());
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
  input.addEventListener("change", () => {
    if (input.files[0]) handleFile(input.files[0]);
  });

  function handleFile(file) {
    onFile(file);
    if (preview && file.type.startsWith("image/")) {
      preview.src = URL.createObjectURL(file);
      preview.classList.remove("hidden");
      zone.querySelector(".dropzone-inner")?.classList.add("hidden");
    }
  }
}

$$(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-btn").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    $(`#tab-${tab}`).classList.add("active");
    const titles = {
      extract: ["Extraction", "Extraction automatique de champs sur vos documents"],
      stats: ["Statistiques", "Monitoring en temps réel"],
    };
    $("#pageTitle").textContent = titles[tab][0];
    $("#pageSubtitle").textContent = titles[tab][1];
    if (tab === "stats") loadStats();
  });
});

let extractFile = null;
setupDropzone("#extractDropzone", "#extractFile", "#extractPreview", (f) => {
  extractFile = f;
  $("#extractSubmit").disabled = false;
});

$("#extractSubmit").addEventListener("click", async () => {
  if (!extractFile) return;
  const results = $("#extractResults");
  results.className = "results loading";
  results.innerHTML = '<div class="spinner"></div><span>Extraction en cours…</span>';
  $("#extractSubmit").disabled = true;

  const fd = new FormData();
  fd.append("file", extractFile);
  const verso = $("#extractVerso").files[0];
  if (verso) fd.append("verso", verso);

  try {
    const res = await api("/ocr/gemini", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Erreur d'extraction");
    results.className = "results";
    const dtype = data.fields?.document_type || "DOCUMENT";
    results.innerHTML = `<span class="badge ok">${escapeHtml(dtype)}</span>`;
    const grid = document.createElement("div");
    results.appendChild(grid);
    renderFields(grid, data.fields);
    if (data.raw_text) {
      const raw = document.createElement("pre");
      raw.className = "raw-text";
      raw.textContent = data.raw_text.slice(0, 800) + (data.raw_text.length > 800 ? "…" : "");
      results.appendChild(raw);
    }
  } catch (e) {
    results.className = "results empty";
    results.innerHTML = `<p style="color:var(--danger)">${escapeHtml(e.message)}</p>`;
    toast(e.message, true);
  } finally {
    $("#extractSubmit").disabled = !extractFile;
  }
});

async function loadStats() {
  try {
    const res = await api("/stats");
    const d = await res.json();
    if (!res.ok) throw new Error("Stats indisponibles");
    $("#statTotal").textContent = d.total_documents ?? 0;
    $("#statSuccess").textContent = `${((d.success_rate || 0) * 100).toFixed(0)} %`;
    $("#statLatency").textContent = `${d.avg_processing_time_ms ?? 0} ms`;
    $("#statQueue").textContent = d.queue_depth ?? 0;
  } catch (e) {
    toast(e.message, true);
  }
}
$("#statsRefresh").addEventListener("click", loadStats);

(async () => {
  try {
    await login();
    const h = await fetch("/health");
    if (h.ok) setStatus(true, "Connecté · demo_client");
    else setStatus(false, "API indisponible");
  } catch {
    setStatus(false, "Connexion échouée");
    toast("Impossible de se connecter à l'API", true);
  }
})();
