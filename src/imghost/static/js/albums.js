const albumsRoot = document.getElementById("owned-albums");
const emptyState = document.getElementById("owned-albums-empty");
const refreshButton = document.getElementById("refresh-albums");
const albumsSummary = document.getElementById("owned-albums-summary");
const albumsStatus = document.getElementById("owned-albums-status");
const albumsPrev = document.getElementById("owned-albums-prev");
const albumsNext = document.getElementById("owned-albums-next");
const albumsPaginationStatus = document.getElementById("owned-albums-pagination-status");

const formatBytes = window.albumCardFormatBytes;
const state = { limit: 10, offset: 0, total: 0 };

const setInlineStatus = (node, message = "") => {
  if (!node) {
    return;
  }
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
};

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

const refreshAlbums = async () => {
  setInlineStatus(albumsStatus);
  setInlineStatus(albumsPaginationStatus);
  const payload = await requestJson(`/api/v1/user/me/albums?limit=${state.limit}&offset=${state.offset}`);
  const albums = payload.items || [];
  state.total = payload.total || 0;
  albumsRoot.innerHTML = albums.length ? albums.map((album) => window.renderAlbumCard(album, {
    metaText: (entry) => `${entry.item_count} item(s) · ${formatBytes(entry.total_size)} · updated ${new Date(entry.updated_at).toLocaleString()}`,
    actions: (entry) => `
      ${window.albumCardPublicLinkControl(entry)}
      <div class="album-card-secondary-actions">
        <a class="button-link secondary-link" href="/api/v1/album/${entry.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
        <button type="button" class="danger album-card-delete" data-album-id="${entry.id}">Delete Album</button>
      </div>
    `,
    actionsClass: "album-card-actions-stack",
  })).join("") : "";
  emptyState?.classList.toggle("hidden", albums.length > 0);
  const start = payload.total === 0 ? 0 : payload.offset + 1;
  const end = payload.offset + albums.length;
  if (albumsSummary) {
    albumsSummary.textContent = payload.total ? `Showing ${start}-${end} of ${payload.total} albums` : "No albums found.";
  }
  if (albumsPrev) {
    albumsPrev.disabled = payload.offset <= 0;
  }
  if (albumsNext) {
    albumsNext.disabled = !payload.has_more;
  }
};

albumsRoot?.addEventListener("click", async (event) => {
  const deleteButton = event.target.closest(".album-card-delete");
  if (!deleteButton) {
    return;
  }
  const albumId = deleteButton.dataset.albumId;
  if (!albumId || !window.confirm("Delete this album?")) {
    return;
  }
  try {
    await requestJson(`/api/v1/album/${albumId}`, { method: "DELETE" });
    if (state.offset > 0 && albumsRoot?.children.length === 1) {
      state.offset = Math.max(0, state.offset - state.limit);
    }
    await refreshAlbums();
  } catch (error) {
    window.alert(error.message);
  }
});

refreshButton?.addEventListener("click", async () => {
  try {
    await refreshAlbums();
  } catch (error) {
    setInlineStatus(albumsStatus, error.message);
  }
});

albumsPrev?.addEventListener("click", async () => {
  state.offset = Math.max(0, state.offset - state.limit);
  try {
    await refreshAlbums();
  } catch (error) {
    setInlineStatus(albumsPaginationStatus, error.message);
  }
});

albumsNext?.addEventListener("click", async () => {
  state.offset += state.limit;
  try {
    await refreshAlbums();
  } catch (error) {
    setInlineStatus(albumsPaginationStatus, error.message);
  }
});

refreshAlbums().catch((error) => {
  setInlineStatus(albumsStatus, error.message);
});
window.attachAlbumCardNavigation?.(albumsRoot);
