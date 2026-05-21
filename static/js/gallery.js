/* gallery.js - Gallery listing, URL-synced filters, search, pagination */

const GALLERY_PER_PAGE = 20;
const GALLERY_PRESET_LABELS = {
  shared: "Only shared",
  mine: "Only mine",
  videos: "Only videos",
  auto: "Only automatic",
};
let searchDebounceTimer = null;

const RTF = (typeof Intl !== "undefined" && Intl.RelativeTimeFormat)
  ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
  : null;

function formatRelative(ts) {
  if (!ts) return "";
  const target = new Date(parseFloat(ts) * 1000);
  const now = Date.now();
  const diffMs = target.getTime() - now;
  const absSec = Math.abs(diffMs / 1000);
  if (!RTF || absSec > 60 * 60 * 24 * 6) {
    return target.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }
  const units = [
    ["year", 60 * 60 * 24 * 365],
    ["month", 60 * 60 * 24 * 30],
    ["day", 60 * 60 * 24],
    ["hour", 60 * 60],
    ["minute", 60],
    ["second", 1],
  ];
  for (const [unit, sec] of units) {
    if (absSec >= sec || unit === "second") {
      return RTF.format(Math.round(diffMs / 1000 / sec), unit);
    }
  }
  return target.toLocaleString();
}

function formatExact(ts) {
  if (!ts) return "";
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return Math.round(bytes / 1024) + " KB";
}

function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const total = Math.round(parseFloat(seconds));
  if (!Number.isFinite(total) || total <= 0) return "";
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${mm}:${String(ss).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function metadataDescription(item) {
  try {
    const data = item && item.metadata ? JSON.parse(item.metadata) : {};
    return String(data.description || "").trim();
  } catch (_) {
    return "";
  }
}

function getStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const page = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
  const type = params.get("type") || "";
  const sort = params.get("sort") === "oldest" ? "oldest" : "newest";
  const q = (params.get("q") || "").trim();
  const preset = params.get("preset");
  const normalizedPreset = Object.prototype.hasOwnProperty.call(GALLERY_PRESET_LABELS, preset) ? preset : "";
  return {
    page,
    type: normalizedPreset === "videos" && type !== "video" ? "video" : type,
    sort,
    q,
    preset: normalizedPreset,
  };
}

function buildParams(state) {
  const params = new URLSearchParams();
  if (state.page > 1) params.set("page", String(state.page));
  if (state.type) params.set("type", state.type);
  if (state.sort && state.sort !== "newest") params.set("sort", state.sort);
  if (state.q) params.set("q", state.q);
  if (state.preset) params.set("preset", state.preset);
  return params;
}

function syncControls(state) {
  document.getElementById("type-filter").value = state.type;
  document.getElementById("sort-filter").value = state.sort;
  document.getElementById("search-filter").value = state.q;
  document.querySelectorAll("[data-type-value]").forEach((btn) => {
    const active = (btn.dataset.typeValue || "") === (state.type || "");
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-sort-value]").forEach((btn) => {
    const active = btn.dataset.sortValue === state.sort;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-preset]").forEach((btn) => {
    const active = btn.dataset.preset === state.preset;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function updateUrl(state, replace) {
  const params = buildParams(state).toString();
  const nextUrl = params ? ("/gallery?" + params) : "/gallery";
  if (replace) history.replaceState(state, "", nextUrl);
  else history.pushState(state, "", nextUrl);
}

function thumbnailEl(item, isImage) {
  const wrap = document.createElement("div");
  wrap.className = "gallery-card-media";

  const img = document.createElement("img");
  img.className = "gallery-card-thumb";
  img.src = "/api/recordings/" + item.id + "/thumbnail";
  img.alt = (isImage ? "Image " : "Video ") + item.id;
  img.loading = "lazy";
  img.onerror = function () {
    const ph = document.createElement("div");
    ph.className = "gallery-card-thumb-placeholder";
    ph.textContent = isImage ? "Image" : "Video";
    img.replaceWith(ph);
  };
  wrap.appendChild(img);

  // Type icon — image or video — overlaid top-left
  const typeBadge = document.createElement("span");
  typeBadge.className = "gallery-thumb-type";
  typeBadge.innerHTML = isImage
    ? '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="M21 17l-5-5-9 9"/></svg>'
    : '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="5 4 19 12 5 20 5 4"/></svg>';
  typeBadge.appendChild(document.createTextNode(isImage ? " IMG" : " VIDEO"));
  wrap.appendChild(typeBadge);

  // Auto badge top-right
  if (item.auto) {
    const autoBadge = document.createElement("span");
    autoBadge.className = "gallery-thumb-auto";
    autoBadge.textContent = item.auto_trigger ? `Auto · ${item.auto_trigger}` : "Auto";
    wrap.appendChild(autoBadge);
  }

  // Duration bottom-right for videos
  const duration = formatDuration(item.duration_seconds);
  if (!isImage && duration) {
    const durEl = document.createElement("span");
    durEl.className = "gallery-thumb-duration";
    durEl.textContent = duration;
    wrap.appendChild(durEl);
  }

  return wrap;
}

function pillEl(text, tone) {
  const el = document.createElement("span");
  el.className = "gallery-pill" + (tone ? " gallery-pill-" + tone : "");
  el.textContent = text;
  return el;
}

function renderCard(item, state) {
  const card = document.createElement("a");
  card.className = "gallery-card";
  const detailParams = buildParams(state).toString();
  card.href = "/gallery/" + item.id + (detailParams ? ("?" + detailParams) : "");

  const isImage = item.type === "image";
  card.appendChild(thumbnailEl(item, isImage));

  const info = document.createElement("div");
  info.className = "gallery-card-info";

  const titleRow = document.createElement("div");
  titleRow.className = "gallery-card-title-row";
  const title = document.createElement("span");
  title.className = "gallery-card-id";
  title.textContent = "#" + item.id;
  titleRow.appendChild(title);
  const time = document.createElement("span");
  time.className = "gallery-card-time";
  time.textContent = formatRelative(item.created_at);
  time.title = formatExact(item.created_at);
  titleRow.appendChild(time);
  info.appendChild(titleRow);

  const description = metadataDescription(item);
  if (description) {
    const descEl = document.createElement("div");
    descEl.className = "gallery-card-desc";
    descEl.textContent = description.length > 110 ? description.slice(0, 107) + "..." : description;
    info.appendChild(descEl);
  }

  const meta = document.createElement("div");
  meta.className = "gallery-card-meta";
  const owner = document.createElement("span");
  owner.className = "gallery-card-meta-item";
  owner.textContent = item.username || "—";
  meta.appendChild(owner);
  const size = formatSize(item.size_bytes);
  if (size) {
    const sep = document.createElement("span");
    sep.className = "gallery-card-meta-sep";
    sep.textContent = "·";
    meta.appendChild(sep);
    const sizeEl = document.createElement("span");
    sizeEl.className = "gallery-card-meta-item";
    sizeEl.textContent = size;
    meta.appendChild(sizeEl);
  }
  info.appendChild(meta);

  const pills = document.createElement("div");
  pills.className = "gallery-card-pills";
  if (item.shared) pills.appendChild(pillEl("Shared", "success"));
  if (item.raw_filename) pills.appendChild(pillEl("Raw + Overlay", "info"));
  else if (item.overlay_filename) pills.appendChild(pillEl("Overlay", "neutral"));
  if (pills.childNodes.length) info.appendChild(pills);

  card.appendChild(info);
  return card;
}

function renderPagination(current, total, onPage) {
  const container = document.getElementById("pagination");
  container.innerHTML = "";
  if (total <= 1) return;

  const mkBtn = (label, page, opts = {}) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pagination-btn" + (opts.active ? " active" : "") + (opts.ghost ? " ghost" : "");
    btn.disabled = page < 1 || page > total;
    if (opts.html) btn.innerHTML = label;
    else btn.textContent = label;
    btn.onclick = () => onPage(page);
    return btn;
  };

  container.appendChild(mkBtn(
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
    current - 1, { ghost: true, html: true }
  ));
  const startPage = Math.max(1, current - 2);
  const endPage = Math.min(total, current + 2);
  for (let page = startPage; page <= endPage; page++) {
    container.appendChild(mkBtn(String(page), page, { active: page === current }));
  }
  container.appendChild(mkBtn(
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>',
    current + 1, { ghost: true, html: true }
  ));
}

function renderSummary(data, state) {
  const summary = document.getElementById("gallery-summary");
  if (!summary) return;
  const parts = [];
  parts.push(data.total + (data.total === 1 ? " recording" : " recordings"));
  if (state.preset) parts.push(GALLERY_PRESET_LABELS[state.preset].toLowerCase());
  if (state.q) parts.push('matching "' + state.q + '"');
  summary.textContent = parts.join(" · ");
}

function getCurrentState() {
  return getStateFromUrl();
}

function readControls(baseState = getCurrentState()) {
  const type = document.getElementById("type-filter").value;
  let preset = baseState.preset || "";
  if (preset === "videos" && type !== "video") {
    preset = "";
  }
  return {
    page: 1,
    type,
    sort: document.getElementById("sort-filter").value,
    q: document.getElementById("search-filter").value.trim(),
    preset,
  };
}

function applyPreset(preset) {
  const current = getCurrentState();
  const next = readControls(current);
  next.page = 1;
  if (next.preset === preset) {
    next.preset = "";
    if (preset === "videos") next.type = "";
  } else {
    next.preset = preset;
    if (preset === "videos") next.type = "video";
  }
  loadGallery(next);
}

function emptyState(message) {
  return `
    <div class="gallery-empty">
      <svg viewBox="0 0 64 64" width="48" height="48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="8" y="14" width="48" height="36" rx="4"/>
        <circle cx="22" cy="28" r="4"/>
        <path d="M56 42L42 28 18 50"/>
      </svg>
      <p class="gallery-empty-text">${escapeHtml(message)}</p>
    </div>
  `;
}

async function loadGallery(state, options = {}) {
  const replace = !!options.replace;
  const skipUrl = !!options.skipUrl;
  const grid = document.getElementById("gallery-grid");
  grid.innerHTML = emptyState("Loading recordings…");

  syncControls(state);
  if (!skipUrl) updateUrl(state, replace);

  const params = buildParams({ ...state, per_page: GALLERY_PER_PAGE });
  params.set("per_page", String(GALLERY_PER_PAGE));

  try {
    const resp = await fetch("/gallery/data?" + params.toString(), { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();

    grid.innerHTML = "";
    renderSummary(data, state);

    if (!data.items || data.items.length === 0) {
      grid.innerHTML = emptyState(
        state.q || state.preset || state.type
          ? "No recordings match the current filters."
          : "No recordings yet. Capture one from the Live page."
      );
      document.getElementById("pagination").innerHTML = "";
      return;
    }

    for (const item of data.items) {
      grid.appendChild(renderCard(item, state));
    }

    renderPagination(data.page, data.pages, (page) => loadGallery({ ...state, page }));
  } catch (err) {
    document.getElementById("gallery-summary").textContent = "Gallery load failed";
    grid.innerHTML = emptyState("Failed to load gallery: " + err.message);
  }
}

function bindSegment(selector, hiddenSelectId) {
  const buttons = document.querySelectorAll(selector);
  const select = document.getElementById(hiddenSelectId);
  if (!select || !buttons.length) return;
  const datasetKey = selector.includes("type") ? "typeValue" : "sortValue";
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      const value = btn.dataset[datasetKey] || "";
      select.value = value;
      buttons.forEach(b => {
        const active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-pressed", active ? "true" : "false");
      });
      loadGallery(readControls());
    });
  });
}

function attachEvents() {
  bindSegment("[data-type-value]", "type-filter");
  bindSegment("[data-sort-value]", "sort-filter");

  document.getElementById("refresh-gallery").addEventListener("click", () => loadGallery(getStateFromUrl(), { replace: true }));
  document.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset || ""));
  });

  document.getElementById("search-filter").addEventListener("input", () => {
    if (searchDebounceTimer) window.clearTimeout(searchDebounceTimer);
    searchDebounceTimer = window.setTimeout(() => {
      loadGallery(readControls());
    }, 220);
  });

  window.addEventListener("popstate", () => {
    loadGallery(getStateFromUrl(), { replace: true, skipUrl: true });
  });
}

attachEvents();
loadGallery(getStateFromUrl(), { replace: true });
