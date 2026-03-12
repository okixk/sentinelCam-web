/* gallery.js – Gallery listing, pagination, filtering */

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || '';
}

function formatDate(ts) {
  if (!ts) return '';
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

function formatSize(bytes) {
  if (!bytes) return '';
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  return Math.round(bytes / 1024) + ' KB';
}

function renderCard(item) {
  const card = document.createElement('a');
  card.className = 'gallery-card';
  card.href = '/gallery/' + item.id;

  const isImage = item.type === 'image';
  const thumbUrl = '/api/recordings/' + item.id + '/thumbnail';

  if (isImage) {
    const img = document.createElement('img');
    img.className = 'gallery-card-thumb';
    img.src = thumbUrl;
    img.alt = 'Recording ' + item.id;
    img.onerror = function() {
      const ph = document.createElement('div');
      ph.className = 'gallery-card-thumb-placeholder';
      ph.textContent = '📷';
      img.replaceWith(ph);
    };
    card.appendChild(img);
  } else {
    const ph = document.createElement('div');
    ph.className = 'gallery-card-thumb-placeholder';
    ph.textContent = '🎥';
    card.appendChild(ph);
  }

  const info = document.createElement('div');
  info.className = 'gallery-card-info';

  const typeEl = document.createElement('div');
  typeEl.className = 'gallery-type';
  let label = (isImage ? '📷 Image' : '🎥 Video') + ' · ' + formatSize(item.size_bytes);
  if (item.shared) label += ' · 🔓';
  typeEl.textContent = label;
  info.appendChild(typeEl);

  const dateEl = document.createElement('div');
  dateEl.className = 'gallery-date';
  dateEl.textContent = formatDate(item.created_at);
  info.appendChild(dateEl);

  if (item.username) {
    const userEl = document.createElement('div');
    userEl.className = 'gallery-date';
    userEl.textContent = '👤 ' + item.username;
    info.appendChild(userEl);
  }

  card.appendChild(info);
  return card;
}

function renderPagination(current, total, onPage) {
  const container = document.getElementById('pagination');
  container.innerHTML = '';
  if (total <= 1) return;

  const mkBtn = (label, page, active) => {
    const btn = document.createElement('button');
    btn.textContent = label;
    if (active) btn.className = 'active';
    btn.disabled = page < 1 || page > total;
    btn.onclick = () => onPage(page);
    return btn;
  };

  container.appendChild(mkBtn('«', current - 1, false));

  const startPage = Math.max(1, current - 2);
  const endPage = Math.min(total, current + 2);
  for (let p = startPage; p <= endPage; p++) {
    container.appendChild(mkBtn(p, p, p === current));
  }

  container.appendChild(mkBtn('»', current + 1, false));
}

async function loadGallery(page = 1) {
  const typeFilter = document.getElementById('type-filter').value;
  const sortFilter = document.getElementById('sort-filter').value;

  const params = new URLSearchParams({ page, per_page: 20, sort: sortFilter });
  if (typeFilter) params.set('type', typeFilter);

  const grid = document.getElementById('gallery-grid');
  grid.innerHTML = '<div style="color:var(--muted); padding:24px;">Loading…</div>';

  try {
    const resp = await fetch('/api/recordings?' + params, { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();

    grid.innerHTML = '';
    if (!data.items || data.items.length === 0) {
      grid.innerHTML = '<div style="color:var(--muted); padding:24px;">No recordings yet.</div>';
      document.getElementById('pagination').innerHTML = '';
      return;
    }

    for (const item of data.items) {
      grid.appendChild(renderCard(item));
    }

    renderPagination(data.page, data.pages, loadGallery);
  } catch (err) {
    grid.innerHTML = '<div style="color:var(--danger); padding:24px;">Failed to load gallery: ' + err.message + '</div>';
  }
}

// Initial load
loadGallery(1);

// Filter change listeners
document.getElementById('type-filter').addEventListener('change', () => loadGallery(1));
document.getElementById('sort-filter').addEventListener('change', () => loadGallery(1));
