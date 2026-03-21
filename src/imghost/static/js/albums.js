const flash = document.getElementById("flash");
const albumsRoot = document.getElementById("owned-albums");
const emptyState = document.getElementById("owned-albums-empty");
const refreshButton = document.getElementById("refresh-albums");

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

const renderAlbum = (album) => `
  <section class="album-card album-list-card">
    <div class="album-workspace-header">
      <div>
        <h3>${escapeHtml(album.title || "Untitled album")}</h3>
        <p class="hint">Album ${escapeHtml(album.id)} · ${album.item_count} item(s) · ${formatBytes(album.total_size)} · updated ${new Date(album.updated_at).toLocaleString()}</p>
      </div>
      <div class="row row-actions">
        <a class="button-link secondary-link" href="/albums/${album.id}">Open Album</a>
        <a class="button-link secondary-link" href="/a/${album.id}" target="_blank" rel="noreferrer">Public Page</a>
        <a class="button-link secondary-link" href="/api/v1/album/${album.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
      </div>
    </div>
  </section>
`;

const refreshAlbums = async () => {
  const albums = await requestJson("/api/v1/user/me/albums");
  albumsRoot.innerHTML = albums.length ? albums.map(renderAlbum).join("") : "";
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
