const flash = document.getElementById("flash");
const bootstrapNode = document.getElementById("album-detail-bootstrap");
const titleNode = document.getElementById("album-detail-title");
const summaryNode = document.getElementById("album-detail-summary");
const actionsNode = document.getElementById("album-detail-actions");
const metadataForm = document.getElementById("album-detail-metadata-form");
const titleInput = document.getElementById("album-detail-title-input");
const coverInput = document.getElementById("album-detail-cover-input");
const deleteButton = document.getElementById("album-detail-delete");
const itemsRoot = document.getElementById("album-detail-items");
const bootstrap = bootstrapNode ? JSON.parse(bootstrapNode.textContent || "{}") : {};

const state = {
  albumId: bootstrap.album_id || null,
  album: null,
};

const showMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
  }
};

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

const formatBytes = (value) => {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const scaled = bytes / (1024 ** exponent);
  const decimals = scaled >= 10 || exponent === 0 ? 0 : 1;
  return `${scaled.toFixed(decimals)} ${units[exponent]}`;
};

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

const publicAlbumUrl = (albumId) => `${window.location.origin}/a/${albumId}`;

const renderActions = (album) => {
  const publicUrl = publicAlbumUrl(album.id);
  actionsNode.innerHTML = `
    <div class="split-link-control">
      <a class="split-link-action" href="${publicUrl}" target="_blank" rel="noreferrer">Public Page</a>
      <input
        class="split-link-value"
        type="text"
        value="${escapeHtml(publicUrl)}"
        readonly
        aria-label="Public page URL"
        onclick="this.select()"
      >
    </div>
    <a class="button-link secondary-link" href="/api/v1/album/${album.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
  `;
};

const previewMarkup = (item) => {
  return `<img src="${escapeHtml(item.media_url)}" alt="${escapeHtml(item.filename)}">`;
};

const renderAlbum = (album) => {
  state.album = album;
  titleNode.textContent = album.title || "Untitled album";
  summaryNode.textContent = `Album ${album.id} · ${album.item_count} item(s) · ${formatBytes(album.total_size)} · updated ${new Date(album.updated_at).toLocaleString()}`;
  renderActions(album);
  metadataForm?.classList.remove("hidden");
  if (titleInput) {
    titleInput.value = album.title || "";
  }
  if (coverInput) {
    coverInput.value = album.cover_media_id || "";
  }
  itemsRoot.innerHTML = album.items.map((item) => `
    <section class="album-item album-detail-item${album.cover_media_id === item.id ? " is-cover" : ""}">
      <a class="album-detail-thumb" href="${escapeHtml(item.media_url)}" target="_blank" rel="noreferrer">
        ${previewMarkup(item)}
      </a>
      <div class="album-item-meta">
        <h3>${escapeHtml(item.filename || item.id)}</h3>
        <p class="hint">${escapeHtml(item.id)} · ${formatBytes(item.file_size)}</p>
        <p class="hint">
          <a class="inline-link" href="${escapeHtml(item.media_url)}" target="_blank" rel="noreferrer">media</a>
        </p>
      </div>
      <div class="row row-actions">
        <button type="button" class="secondary album-detail-cover-button" data-media-id="${item.id}">Set As Cover</button>
        <button type="button" class="danger album-detail-delete-media" data-media-id="${item.id}">Delete Media</button>
      </div>
    </section>
  `).join("");
};

const loadAlbum = async () => {
  if (!state.albumId) {
    showMessage("Missing album ID.");
    return;
  }
  const album = await requestJson(`/api/v1/album/${state.albumId}`);
  renderAlbum(album);
};

metadataForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.albumId) {
    return;
  }
  try {
    const album = await requestJson(`/api/v1/album/${state.albumId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: titleInput?.value || null,
        cover_media_id: coverInput?.value || null,
      }),
    });
    renderAlbum(album);
    showMessage("Album updated.");
  } catch (error) {
    showMessage(error.message);
  }
});

deleteButton?.addEventListener("click", async () => {
  if (!state.albumId || !window.confirm("Delete this album?")) {
    return;
  }
  try {
    await requestJson(`/api/v1/album/${state.albumId}`, { method: "DELETE" });
    window.location.assign("/albums");
  } catch (error) {
    showMessage(error.message);
  }
});

itemsRoot?.addEventListener("click", async (event) => {
  const coverButton = event.target.closest(".album-detail-cover-button");
  if (coverButton && state.albumId) {
    try {
      const album = await requestJson(`/api/v1/album/${state.albumId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cover_media_id: coverButton.dataset.mediaId }),
      });
      renderAlbum(album);
      showMessage("Cover updated.");
    } catch (error) {
      showMessage(error.message);
    }
    return;
  }

  const deleteMediaButton = event.target.closest(".album-detail-delete-media");
  if (deleteMediaButton) {
    if (!window.confirm("Delete this media item?")) {
      return;
    }
    try {
      const result = await requestJson(`/api/v1/media/${deleteMediaButton.dataset.mediaId}`, { method: "DELETE" });
      if (result.album_deleted) {
        window.location.assign("/albums");
        return;
      }
      await loadAlbum();
      showMessage("Media deleted.");
    } catch (error) {
      showMessage(error.message);
    }
  }
});

loadAlbum().catch((error) => {
  showMessage(error.message);
  titleNode.textContent = "Album unavailable";
  summaryNode.textContent = "This owner album could not be loaded.";
});
