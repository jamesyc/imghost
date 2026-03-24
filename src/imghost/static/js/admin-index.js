const adminOverviewStats = document.getElementById("admin-overview-stats");
const adminOverviewRuntime = document.getElementById("admin-overview-runtime");
const adminOverviewNetworkTrust = document.getElementById("admin-overview-network-trust");
const adminOverviewStatsStatus = document.getElementById("admin-overview-stats-status");
const adminOverviewRuntimeStatus = document.getElementById("admin-overview-runtime-status-text");

if (adminOverviewStats && adminOverviewRuntime) {
  const statusTone = (ok) => (ok ? "ok" : "warn");
  const formatPercent = (value) => {
    if (value == null || !Number.isFinite(Number(value))) {
      return "No limit";
    }
    return `${Math.round(Number(value))}%`;
  };

  const usageWidth = (value) => {
    if (value == null || !Number.isFinite(Number(value))) {
      return 0;
    }
    return Math.max(0, Math.min(100, Number(value)));
  };

  const renderMetric = (label, value, hint = "") => `
    <article class="admin-metric-card">
      <p class="eyebrow">${label}</p>
      <h3>${window.escapeAdminHtml(value)}</h3>
      ${hint ? `<p class="hint">${window.escapeAdminHtml(hint)}</p>` : ""}
    </article>
  `;

  const renderQuotaProgress = (label, usedBytes, quotaBytes, quotaPercent, remainingBytes, unlimited) => `
    <section class="admin-overview-subsection admin-storage-breakdown-card">
      <div class="admin-section-header">
        <h3>${window.escapeAdminHtml(label)}</h3>
        <p class="hint">
          ${
            unlimited
              ? `${window.adminFormatBytes(usedBytes)} used with no quota limit set.`
              : `${window.adminFormatBytes(usedBytes)} of ${window.adminFormatBytes(quotaBytes)} used · ${formatPercent(quotaPercent)}`
          }
        </p>
      </div>
      <div class="usage-meter" aria-hidden="true">
        <span class="usage-meter-bar" style="width: ${usageWidth(quotaPercent)}%;"></span>
      </div>
      ${
        unlimited
          ? ""
          : `<p class="hint">${window.adminFormatBytes(remainingBytes)} remaining before the quota is full.</p>`
      }
    </section>
  `;

  const renderUserStorageTable = (users) => `
    <section class="admin-overview-subsection admin-storage-breakdown-card">
      <div class="admin-section-header">
        <h3>Largest accounts</h3>
        <p class="hint">Top 10 accounts by storage used. Use the Users page for the full paginated breakdown.</p>
      </div>
      <div class="admin-storage-table" role="table" aria-label="Per-user storage breakdown">
        <div class="admin-storage-table-row admin-storage-table-header" role="row">
          <strong role="columnheader">User</strong>
          <strong role="columnheader">Usage</strong>
          <strong role="columnheader">Quota</strong>
          <strong role="columnheader">Quota %</strong>
          <strong role="columnheader">Counts</strong>
        </div>
        ${
          users.length
            ? users
                .map(
                  (user) => `
                    <div class="admin-storage-table-row" role="row">
                      <div role="cell">
                        <strong>${window.escapeAdminHtml(user.username)}</strong>
                        <p class="hint">${window.escapeAdminHtml(user.email || "No email")}</p>
                      </div>
                      <div role="cell">
                        <strong>${window.adminFormatBytes(user.storage_used_bytes)}</strong>
                        <div class="usage-meter" aria-hidden="true">
                          <span class="usage-meter-bar" style="width: ${usageWidth(user.quota_percent)}%;"></span>
                        </div>
                      </div>
                      <div role="cell">
                        <strong>${
                          user.quota_unlimited ? "No limit" : window.adminFormatBytes(user.quota_bytes)
                        }</strong>
                        ${
                          user.quota_unlimited
                            ? ""
                            : `<p class="hint">${window.adminFormatBytes(user.quota_remaining_bytes)} remaining</p>`
                        }
                      </div>
                      <div role="cell">
                        <strong>${formatPercent(user.quota_percent)}</strong>
                      </div>
                      <div role="cell">
                        <strong>${window.adminFormatNumber(user.media_count)} files</strong>
                        <p class="hint">${window.adminFormatNumber(user.album_count)} albums</p>
                      </div>
                    </div>
                  `
                )
                .join("")
            : '<p class="hint">No user storage data yet.</p>'
        }
      </div>
    </section>
  `;

  const renderStats = (payload) => {
    const users = Array.isArray(payload.users) ? payload.users.slice(0, 10) : [];
    adminOverviewStats.innerHTML = `
      <div class="admin-metric-grid">
        ${renderMetric("Server quota", payload.server_quota_unlimited ? "No limit" : window.adminFormatBytes(payload.server_quota_bytes))}
        ${renderMetric("Total used", window.adminFormatBytes(payload.total_storage_used_bytes))}
        ${renderMetric("Anonymous used", window.adminFormatBytes(payload.anonymous_storage_used_bytes))}
        ${renderMetric("Accounts", window.adminFormatNumber(payload.user_count))}
      </div>
      ${renderQuotaProgress(
        "Server quota",
        payload.total_storage_used_bytes,
        payload.server_quota_bytes,
        payload.server_quota_percent,
        payload.server_quota_remaining_bytes,
        payload.server_quota_unlimited,
      )}
      ${renderUserStorageTable(users)}
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
