const adminOverviewStats = document.getElementById("admin-overview-stats");
const adminOverviewRuntime = document.getElementById("admin-overview-runtime");
const adminOverviewNetworkTrust = document.getElementById("admin-overview-network-trust");
const adminOverviewStatsStatus = document.getElementById("admin-overview-stats-status");
const adminOverviewRuntimeStatus = document.getElementById("admin-overview-runtime-status-text");

if (adminOverviewStats && adminOverviewRuntime) {
  const statusTone = (ok) => (ok ? "ok" : "warn");

  const renderMetric = (label, value, hint = "") => `
    <article class="admin-metric-card">
      <p class="eyebrow">${label}</p>
      <h3>${window.escapeAdminHtml(value)}</h3>
      ${hint ? `<p class="hint">${window.escapeAdminHtml(hint)}</p>` : ""}
    </article>
  `;

  const renderStats = (payload) => {
    const topUsers = Array.isArray(payload.users) ? payload.users.slice(0, 4) : [];
    adminOverviewStats.innerHTML = `
      <div class="admin-metric-grid">
        ${renderMetric("Server quota", window.adminFormatBytes(payload.server_quota_bytes))}
        ${renderMetric("Total used", window.adminFormatBytes(payload.total_storage_used_bytes))}
        ${renderMetric("Anonymous used", window.adminFormatBytes(payload.anonymous_storage_used_bytes))}
        ${renderMetric("Accounts", window.adminFormatNumber(payload.user_count))}
      </div>
      <section class="admin-overview-subsection">
        <div class="admin-section-header">
          <h3>Largest accounts</h3>
          <p class="hint">Quick storage view across the heaviest users.</p>
        </div>
        <div class="item-list">
          ${
            topUsers.length
              ? topUsers
                  .map(
                    (user) => `
                      <article class="admin-list-row">
                        <div>
                          <strong>${window.escapeAdminHtml(user.username)}</strong>
                          <p class="hint">${window.escapeAdminHtml(user.email || "No email")}</p>
                        </div>
                        <div class="admin-list-row-meta">
                          <strong>${window.adminFormatBytes(user.storage_used_bytes)}</strong>
                          <p class="hint">${window.adminFormatNumber(user.album_count)} albums · ${window.adminFormatNumber(user.media_count)} files</p>
                        </div>
                      </article>
                    `
                  )
                  .join("")
              : '<p class="hint">No user storage data yet.</p>'
          }
        </div>
      </section>
    `;
  };

  const renderRuntime = (payload) => {
    adminOverviewRuntime.innerHTML = window.renderAdminRuntimeCards(payload);
    if (adminOverviewNetworkTrust) {
      adminOverviewNetworkTrust.innerHTML = window.renderAdminNetworkTrust(payload);
    }
  };

  const refreshStats = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/stats");
    renderStats(payload);
  };

  const refreshRuntime = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/runtime-status");
    renderRuntime(payload);
  };

  document.getElementById("admin-overview-refresh-stats")?.addEventListener("click", () => {
    refreshStats().catch((error) => {
      window.setAdminStatus(adminOverviewStatsStatus, error.message, "error");
    });
  });

  document.getElementById("admin-overview-refresh-runtime")?.addEventListener("click", () => {
    refreshRuntime().catch((error) => {
      window.setAdminStatus(adminOverviewRuntimeStatus, error.message, "error");
    });
  });

  refreshStats().catch((error) => {
    window.setAdminStatus(adminOverviewStatsStatus, error.message, "error");
  });
  refreshRuntime().catch((error) => {
    window.setAdminStatus(adminOverviewRuntimeStatus, error.message, "error");
  });
}
