const adminAlbumsRoot = document.getElementById("admin-albums");
const adminAlbumsSearchForm = document.getElementById("admin-album-search-form");
const adminAlbumsSummary = document.getElementById("admin-albums-summary");
const adminAlbumsPrev = document.getElementById("admin-albums-prev");
const adminAlbumsNext = document.getElementById("admin-albums-next");

if (adminAlbumsRoot) {
  const state = {
    q: "",
    owner: "",
    anonymous: "",
    limit: 10,
    offset: 0,
    total: 0,
  };

  const publicAlbumUrl = (albumId) => `/a/${encodeURIComponent(albumId)}`;
  const adminUserUrl = (userId) => `/admin/users/${encodeURIComponent(userId)}`;
  const previewItem = (album) => {
    if (!Array.isArray(album.items) || !album.items.length) {
      return null;
    }
    if (album.cover_media_id) {
      const coverItem = album.items.find((item) => item.id === album.cover_media_id);
      if (coverItem) {
        return coverItem;
      }
    }
    return album.items[0];
  };

  const renderThumb = (album) => {
    const item = previewItem(album);
    if (!item) {
      return '<span class="dashboard-recent-album-thumb-placeholder">No media</span>';
    }
    if (item.thumb_status === "failed") {
      return '<span class="dashboard-recent-album-thumb-placeholder">Thumbnail failed</span>';
    }
    if (item.thumb_status === "pending" || item.thumb_status === "processing") {
      return '<span class="dashboard-recent-album-thumb-placeholder">Thumbnail pending</span>';
    }
    return `<img src="${window.escapeAdminHtml(item.thumb_url)}" alt="${window.escapeAdminHtml(album.title || "Untitled album")}">`;
  };

  const buildParams = () => {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.owner) params.set("owner", state.owner);
    if (state.anonymous) params.set("anonymous", state.anonymous);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params;
  };

  const renderAlbums = (payload) => {
    const albums = payload.items || [];
    state.total = payload.total || 0;
    adminAlbumsRoot.innerHTML = albums
      .map(
        (album) => `
        <section class="admin-card admin-album-card dashboard-recent-album-card" data-album-id="${album.id}">
          <div class="dashboard-recent-album-copy">
            <div>
              <h3>${window.escapeAdminHtml(album.title || "Untitled album")}</h3>
              <p class="hint">album=${album.id} · owner=${window.escapeAdminHtml(album.owner_username || "anonymous")} · items=${album.item_count}</p>
            </div>
            <div class="row row-actions">
              <a class="button-link secondary-link" href="${publicAlbumUrl(album.id)}" target="_blank" rel="noreferrer">Open Album</a>
              ${
                album.user_id
                  ? `<a class="button-link secondary-link" href="${adminUserUrl(album.user_id)}">Open Owner</a>`
                  : ""
              }
            </div>
            <form class="admin-album-patch-form stack">
              <input type="datetime-local" name="expires_at" value="${album.expires_at ? album.expires_at.slice(0, 16) : ""}">
              <div class="row row-actions">
                <button type="submit">Set/Clear Expiry</button>
                <button type="button" class="danger admin-album-delete">Delete Album</button>
              </div>
            </form>
            <p class="inline-status hidden admin-item-status" aria-live="polite"></p>
          </div>
          <a class="dashboard-recent-album-thumb" href="${publicAlbumUrl(album.id)}" target="_blank" rel="noreferrer" aria-label="Open album ${window.escapeAdminHtml(album.title || album.id)}">
            ${renderThumb(album)}
          </a>
        </section>
      `,
      )
      .join("");
    const start = payload.total === 0 ? 0 : payload.offset + 1;
    const end = payload.offset + albums.length;
    adminAlbumsSummary.textContent = payload.total
      ? `Showing ${start}-${end} of ${payload.total} albums`
      : "No albums found.";
    adminAlbumsPrev.disabled = payload.offset <= 0;
    adminAlbumsNext.disabled = !payload.has_more;
  };

  const refreshAlbums = async () => {
    const payload = await window.adminRequestJson(`/api/v1/admin/albums?${buildParams().toString()}`);
    renderAlbums(payload);
  };

  adminAlbumsSearchForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    state.q = String(form.get("q") || "").trim();
    state.owner = String(form.get("owner") || "").trim();
    state.anonymous = String(form.get("anonymous") || "");
    state.offset = 0;
    try {
      await refreshAlbums();
    } catch (error) {
      adminAlbumsRoot.textContent = error.message;
    }
  });

  adminAlbumsRoot.addEventListener("submit", async (event) => {
    const card = event.target.closest("[data-album-id]");
    if (!card) return;
    event.preventDefault();
    const status = card.querySelector(".admin-item-status");
    window.setAdminStatus(status);
    try {
      const form = new FormData(event.target);
      await window.adminRequestJson(`/api/v1/admin/albums/${card.dataset.albumId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expires_at: window.parseOptionalDate(form.get("expires_at")) }),
      });
      window.setAdminStatus(status, "Album admin metadata updated.", "success");
      await refreshAlbums();
    } catch (error) {
      window.setAdminStatus(status, error.message, "error");
    }
  });

  adminAlbumsRoot.addEventListener("click", async (event) => {
    const card = event.target.closest("[data-album-id]");
    if (!card || !event.target.matches(".admin-album-delete")) return;
    if (!window.confirm(`Delete album ${card.dataset.albumId}?`)) return;
    const status = card.querySelector(".admin-item-status");
    window.setAdminStatus(status);
    try {
      await window.adminRequestJson(`/api/v1/admin/albums/${card.dataset.albumId}`, { method: "DELETE" });
      if (state.offset > 0 && adminAlbumsRoot?.children.length === 1) {
        state.offset = Math.max(0, state.offset - state.limit);
      }
      await refreshAlbums();
    } catch (error) {
      window.setAdminStatus(status, error.message, "error");
    }
  });

  adminAlbumsPrev?.addEventListener("click", async () => {
    state.offset = Math.max(0, state.offset - state.limit);
    try {
      await refreshAlbums();
    } catch (error) {
      adminAlbumsRoot.textContent = error.message;
    }
  });

  adminAlbumsNext?.addEventListener("click", async () => {
    state.offset += state.limit;
    try {
      await refreshAlbums();
    } catch (error) {
      adminAlbumsRoot.textContent = error.message;
    }
  });

  document.getElementById("refresh-admin-albums")?.addEventListener("click", () => {
    refreshAlbums().catch((error) => {
      adminAlbumsRoot.textContent = error.message;
    });
  });

  refreshAlbums().catch((error) => {
    adminAlbumsRoot.textContent = error.message;
  });
}
