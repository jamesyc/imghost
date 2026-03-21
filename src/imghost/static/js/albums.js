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
      <a class="button-link secondary-link" href="/api/v1/album/${entry.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
    `,
    actionsClass: "album-card-actions-stack",
  })).join("") : "";
  emptyState?.classList.toggle("hidden", albums.length > 0);
};

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
