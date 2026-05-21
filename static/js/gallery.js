/* gallery.js - Gallery listing, URL-synced filters, search, pagination */

const GALLERY_PER_PAGE = 20;
const GALLERY_PRESET_LABELS = {
  shared: "Only shared",
  mine: "Only mine",
  videos: "Only videos",
  auto: "Only automatic",
};
let searchDebounceTimer = null;

function formatDate(ts) {
  if (!ts) return "";
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return Math.round(bytes / 1024) + " KB";
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

function renderCard(item, state) {
  const card = document.createElement("a");
  card.className = "gallery-card";
  const detailParams = buildParams(state).toString();
  card.href = "/gallery/" + item.id + (detailParams ? ("?" + detailParams) : "");

  const isImage = item.type === "image";
  const thumbUrl = "/api/recordings/" + item.id + "/thumbnail";

  if (isImage) {
    const img = document.createElement("img");
    img.className = "gallery-card-thumb";
    img.src = thumbUrl;
    img.alt = "Recording " + item.id;
    img.loading = "lazy";
    img.onerror = function () {
      const placeholder = document.createElement("div");
      placeholder.className = "gallery-card-thumb-placeholder";
      placeholder.textContent = "Image";
      img.replaceWith(placeholder);
    };
    card.appendChild(img);
  } else {
    const videoThumb = document.createElement("img");
    videoThumb.className = "gallery-card-thumb";
    videoThumb.src = thumbUrl;
    videoThumb.alt = "Video thumbnail " + item.id;
    videoThumb.loading = "lazy";
    videoThumb.onerror = function () {
      const placeholder = document.createElement("div");
      placeholder.className = "gallery-card-thumb-placeholder";
      placeholder.textContent = "Video";
      videoThumb.replaceWith(placeholder);
    };
    card.appendChild(videoThumb);
  }

  const info = document.createElement("div");
  info.className = "gallery-card-info";

  const typeEl = document.createElement("div");
  typeEl.className = "gallery-type";
  typeEl.textContent = isImage ? "Image" : "Video";
  info.appendChild(typeEl);

  const titleEl = document.createElement("div");
  titleEl.className = "gallery-card-title";
  titleEl.textContent = "Recording #" + item.id;
  info.appendChild(titleEl);

  const metaEl = document.createElement("div");
  metaEl.className = "gallery-date";
  metaEl.innerHTML =
    escapeHtml(formatDate(item.created_at)) +
    " | " +
    escapeHtml(formatSize(item.size_bytes));
  info.appendChild(metaEl);

  const ownerEl = document.createElement("div");
  ownerEl.className = "gallery-date";
  ownerEl.textContent = "Owner: " + (item.username || "-");
  info.appendChild(ownerEl);

  const description = metadataDescription(item);
  if (description) {
    const descEl = document.createElement("div");
    descEl.className = "gallery-date";
    descEl.textContent = description.length > 110 ? description.slice(0, 107) + "..." : description;
    info.appendChild(descEl);
  }

  const variantEl = document.createElement("div");
  variantEl.className = "gallery-card-flags";
  let flags = item.overlay_filename ? "Overlay" : "No overlay";
  if (item.raw_filename) flags += " | Raw";
  if (item.shared) flags += " | Shared";
  if (item.auto) {
    flags += item.auto_trigger ? ` | Auto (${item.auto_trigger})` : " | Auto";
  }
  variantEl.textContent = flags;
  info.appendChild(variantEl);

  card.appendChild(info);
  return card;
}

function renderPagination(current, total, onPage) {
  const container = document.getElementById("pagination");
  container.innerHTML = "";
  if (total <= 1) return;

  const mkBtn = (label, page, active) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (active) btn.className = "active";
    btn.disabled = page < 1 || page > total;
    btn.onclick = () => onPage(page);
    return btn;
  };

  container.appendChild(mkBtn("<", current - 1, false));
  const startPage = Math.max(1, current - 2);
  const endPage = Math.min(total, current + 2);
  for (let page = startPage; page <= endPage; page++) {
    container.appendChild(mkBtn(String(page), page, page === current));
  }
  container.appendChild(mkBtn(">", current + 1, false));
}

function renderSummary(data, state) {
  const summary = document.getElementById("gallery-summary");
  const parts = [];
  parts.push(data.total + (data.total === 1 ? " item" : " items"));
  if (state.preset) parts.push("preset: " + GALLERY_PRESET_LABELS[state.preset].toLowerCase());
  if (state.type && !(state.preset === "videos" && state.type === "video")) parts.push("type: " + state.type);
  if (state.q) parts.push('search: "' + state.q + '"');
  parts.push("sorted: " + state.sort);
  summary.textContent = parts.join(" | ");
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

async function loadGallery(state, options = {}) {
  const replace = !!options.replace;
  const skipUrl = !!options.skipUrl;
  const grid = document.getElementById("gallery-grid");
  grid.innerHTML = '<div class="empty-state">Loading recordings...</div>';

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
      grid.innerHTML = '<div class="empty-state">No recordings found for the current filters.</div>';
      document.getElementById("pagination").innerHTML = "";
      return;
    }

    for (const item of data.items) {
      grid.appendChild(renderCard(item, state));
    }

    renderPagination(data.page, data.pages, (page) => loadGallery({ ...state, page }));
  } catch (err) {
    document.getElementById("gallery-summary").textContent = "Gallery load failed";
    grid.innerHTML = '<div class="empty-state">Failed to load gallery: ' + escapeHtml(err.message) + "</div>";
  }
}

function attachEvents() {
  document.getElementById("type-filter").addEventListener("change", () => loadGallery(readControls()));
  document.getElementById("sort-filter").addEventListener("change", () => loadGallery(readControls()));
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
