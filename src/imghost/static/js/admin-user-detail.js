const adminUserDetailRoot = document.getElementById("admin-user-detail-root");

if (adminUserDetailRoot) {
  const bootstrap = JSON.parse(document.getElementById("admin-user-detail-bootstrap")?.textContent || "{}");
  const userId = bootstrap.user_id || adminUserDetailRoot.dataset.userId;
  const state = {
    albumLimit: 10,
    albumOffset: 0,
    albumTotal: 0,
  };

  const detailStatus = document.getElementById("admin-user-detail-status");
  const statsStatus = document.getElementById("admin-user-stats-status");
  const summaryRoot = document.getElementById("admin-user-detail-summary");
  const statsRoot = document.getElementById("admin-user-detail-stats");
  const albumsRoot = document.getElementById("admin-user-albums");
  const albumsSummary = document.getElementById("admin-user-albums-summary");
  const albumsPrev = document.getElementById("admin-user-albums-prev");
  const albumsNext = document.getElementById("admin-user-albums-next");

  const patchForm = document.getElementById("admin-user-detail-patch-form");
  const resetForm = document.getElementById("admin-user-detail-reset-form");
  const isAdminInput = document.getElementById("admin-user-detail-is-admin");
  const suspendedInput = document.getElementById("admin-user-detail-suspended");
  const quotaInput = document.getElementById("admin-user-detail-quota");
  const rpmInput = document.getElementById("admin-user-detail-rpm");
  const bphInput = document.getElementById("admin-user-detail-bph");

  const renderSummary = (payload) => {
    document.getElementById("admin-user-detail-name").textContent = payload.username;
    isAdminInput.checked = Boolean(payload.is_admin);
    suspendedInput.checked = Boolean(payload.suspended);
    quotaInput.value = payload.quota_bytes ?? "";
    rpmInput.value = payload.rate_limit_rpm ?? "";
    bphInput.value = payload.rate_limit_bph ?? "";
    summaryRoot.innerHTML = `
      <div class="admin-metric-grid">
        <article class="admin-metric-card">
          <p class="eyebrow">Identity</p>
          <h3>${window.escapeAdminHtml(payload.username)}</h3>
          <p class="hint">${window.escapeAdminHtml(payload.email)}</p>
        </article>
        <article class="admin-metric-card">
          <p class="eyebrow">Role</p>
          <h3>${payload.is_admin ? "Admin" : "User"}</h3>
          <p class="hint">${payload.suspended ? "Suspended" : "Active"}</p>
        </article>
        <article class="admin-metric-card">
          <p class="eyebrow">Created</p>
          <h3>${window.adminFormatDateTime(payload.created_at)}</h3>
          <p class="hint">User ID ${window.escapeAdminHtml(payload.id)}</p>
        </article>
      </div>
    `;
  };

  const renderStats = (payload) => {
    statsRoot.innerHTML = `
      <div class="admin-metric-grid">
        <article class="admin-metric-card">
          <p class="eyebrow">Storage used</p>
          <h3>${window.adminFormatBytes(payload.storage_used_bytes)}</h3>
          <p class="hint">${window.adminFormatNumber(payload.media_count)} media files</p>
        </article>
        <article class="admin-metric-card">
          <p class="eyebrow">Quota</p>
          <h3>${window.adminFormatBytes(payload.quota_bytes)}</h3>
          <p class="hint">${window.adminFormatNumber(payload.album_count)} albums</p>
        </article>
      </div>
    `;
  };

  const renderAlbums = (payload) => {
    const albums = payload.items || [];
    state.albumTotal = payload.total || 0;
    albumsRoot.innerHTML = albums.length
      ? albums
          .map(
            (album) =>
              window.renderAlbumCard(album, {
                openUrl: (entry) => `/a/${encodeURIComponent(entry.id)}`,
                thumbHref: (entry) => `/a/${encodeURIComponent(entry.id)}`,
                metaText: (entry) =>
                  `${window.adminFormatNumber(entry.item_count)} item(s) · ${window.adminFormatBytes(entry.total_size)} · updated ${window.adminFormatDateTime(entry.updated_at)}`,
              })
          )
          .join("")
      : '<p class="hint">This user does not own any albums yet.</p>';
    const start = payload.total === 0 ? 0 : payload.offset + 1;
    const end = payload.offset + albums.length;
    albumsSummary.textContent = payload.total
      ? `Showing ${start}-${end} of ${payload.total} albums`
      : "No albums found.";
    albumsPrev.disabled = payload.offset <= 0;
    albumsNext.disabled = !payload.has_more;
    window.attachAlbumCardNavigation?.(albumsRoot);
  };

  const refreshSummary = async () => {
    const payload = await window.adminRequestJson(`/api/v1/admin/users/${userId}`);
    renderSummary(payload);
  };

  const refreshStats = async () => {
    const payload = await window.adminRequestJson(`/api/v1/admin/users/${userId}/stats`);
    renderStats(payload);
  };

  const refreshAlbums = async () => {
    const params = new URLSearchParams({
      limit: String(state.albumLimit),
      offset: String(state.albumOffset),
    });
    const payload = await window.adminRequestJson(`/api/v1/admin/users/${userId}/albums?${params.toString()}`);
    renderAlbums(payload);
  };

  const refreshPage = async () => {
    await Promise.all([refreshSummary(), refreshStats(), refreshAlbums()]);
  };

  patchForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    window.setAdminStatus(detailStatus);
    try {
      await window.adminRequestJson(`/api/v1/admin/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_admin: isAdminInput.checked,
          suspended: suspendedInput.checked,
          quota_bytes: window.parseOptionalNumber(quotaInput.value),
          rate_limit_rpm: window.parseOptionalNumber(rpmInput.value),
          rate_limit_bph: window.parseOptionalNumber(bphInput.value),
        }),
      });
      window.setAdminStatus(detailStatus, "User updated.", "success");
      await Promise.all([refreshSummary(), refreshStats()]);
    } catch (error) {
      window.setAdminStatus(detailStatus, error.message, "error");
    }
  });

  resetForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    window.setAdminStatus(detailStatus);
    const form = new FormData(event.currentTarget);
    const newPassword = String(form.get("new_password") || "");
    if (newPassword.length < 8) {
      window.setAdminStatus(detailStatus, "New passwords must be at least 8 characters.", "error");
      return;
    }
    try {
      await window.adminRequestJson(`/api/v1/admin/users/${userId}/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: newPassword }),
      });
      event.currentTarget.reset();
      window.setAdminStatus(detailStatus, "Password reset.", "success");
    } catch (error) {
      window.setAdminStatus(detailStatus, error.message, "error");
    }
  });

  document.getElementById("admin-user-detail-delete")?.addEventListener("click", async () => {
    if (!window.confirm(`Delete user ${userId}?`)) return;
    window.setAdminStatus(detailStatus);
    try {
      await window.adminRequestJson(`/api/v1/admin/users/${userId}`, { method: "DELETE" });
      window.location.assign("/admin/users");
    } catch (error) {
      window.setAdminStatus(detailStatus, error.message, "error");
    }
  });

  document.getElementById("admin-user-detail-refresh")?.addEventListener("click", async () => {
    window.setAdminStatus(statsStatus);
    try {
      await refreshPage();
    } catch (error) {
      window.setAdminStatus(statsStatus, error.message, "error");
    }
  });

  albumsPrev?.addEventListener("click", async () => {
    state.albumOffset = Math.max(0, state.albumOffset - state.albumLimit);
    try {
      await refreshAlbums();
    } catch (error) {
      window.setAdminStatus(statsStatus, error.message, "error");
    }
  });

  albumsNext?.addEventListener("click", async () => {
    state.albumOffset += state.albumLimit;
    try {
      await refreshAlbums();
    } catch (error) {
      window.setAdminStatus(statsStatus, error.message, "error");
    }
  });

  refreshPage().catch((error) => {
    window.setAdminStatus(detailStatus, error.message, "error");
  });
}
