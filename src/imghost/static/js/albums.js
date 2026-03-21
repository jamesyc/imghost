const flash = document.getElementById("flash");
const albumsRoot = document.getElementById("owned-albums");
const emptyState = document.getElementById("owned-albums-empty");
const refreshButton = document.getElementById("refresh-albums");

const showMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
  }
};

const formatBytes = window.albumCardFormatBytes;

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

const refreshAlbums = async () => {
  const albums = await requestJson("/api/v1/user/me/albums");
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
    await refreshAlbums();
    showMessage("Album deleted.");
  } catch (error) {
    showMessage(error.message);
  }
});

refreshButton?.addEventListener("click", async () => {
  try {
    await refreshAlbums();
    showMessage("Albums refreshed.");
  } catch (error) {
    showMessage(error.message);
  }
});

refreshAlbums().catch((error) => {
  showMessage(error.message);
});
window.attachAlbumCardNavigation?.(albumsRoot);
