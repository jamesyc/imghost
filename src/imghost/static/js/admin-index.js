const adminOverviewStats = document.getElementById("admin-overview-stats");
const adminOverviewRuntime = document.getElementById("admin-overview-runtime");
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
    const subsystems = [
      {
        label: "Database",
        value: payload.database?.ok ? "Healthy" : "Unavailable",
        tone: statusTone(payload.database?.ok),
        hint: "Primary application database",
      },
      {
        label: "Storage",
        value: payload.storage?.ok ? "Healthy" : "Unavailable",
        tone: statusTone(payload.storage?.ok),
        hint: "Object storage health check",
      },
      {
        label: "Redis",
        value: payload.redis?.configured ? (payload.redis?.reachable ? "Reachable" : "Configured, not reachable") : "Disabled",
        tone: payload.redis?.configured ? statusTone(payload.redis?.reachable) : "neutral",
        hint: `Sessions ${payload.redis?.subsystems?.sessions?.mode || "unknown"} · Tasks ${payload.redis?.subsystems?.tasks?.mode || "unknown"}`,
      },
      {
        label: "Tasks",
        value: payload.tasks?.mode ? `${payload.tasks.mode} queue` : "Unknown",
        tone: payload.tasks?.queue_depth > 0 ? "warn" : "ok",
        hint: `Queue depth ${window.adminFormatNumber(payload.tasks?.queue_depth || 0)} · Workers ${window.adminFormatNumber(payload.tasks?.active_workers || 0)}`,
      },
      {
        label: "Worker",
        value: payload.worker?.enabled_in_this_process
          ? "Running in app process"
          : payload.tasks?.mode === "redis"
            ? "Separate worker service"
            : "Disabled",
        tone: payload.worker?.enabled_in_this_process || payload.tasks?.mode === "redis"
          ? (payload.worker?.last_task_failure ? "warn" : "ok")
          : "neutral",
        hint: payload.worker?.last_task_failure_at
          ? `Last failure ${window.adminFormatDateTime(payload.worker.last_task_failure_at)}`
          : `Last started ${window.adminFormatDateTime(payload.worker?.last_started_at)}`,
      },
      {
        label: "Proxy policy",
        value: payload.forwarded_headers_policy || "Unknown",
        tone: "neutral",
        hint: payload.trusted_proxy_cidrs_enabled
          ? `${(payload.trusted_proxy_cidrs || []).length} trusted CIDR(s)`
          : "Forwarded headers accepted permissively",
      },
    ];

    adminOverviewRuntime.innerHTML = `
      <div class="admin-runtime-grid">
        ${subsystems
          .map(
            (entry) => `
              <article class="admin-runtime-card">
                <div class="admin-status-pill" data-tone="${entry.tone}">${window.escapeAdminHtml(entry.value)}</div>
                <h3>${window.escapeAdminHtml(entry.label)}</h3>
                <p class="hint">${window.escapeAdminHtml(entry.hint)}</p>
              </article>
            `
          )
          .join("")}
      </div>
      <section class="admin-overview-subsection">
        <div class="admin-section-header">
          <h3>Network trust</h3>
          <p class="hint">Origins and proxy trust that affect request handling.</p>
        </div>
        <div class="item-list">
          <article class="admin-list-row">
            <div>
              <strong>Trusted public origins</strong>
              <p class="hint">${window.escapeAdminHtml((payload.trusted_public_origins || []).join(", ") || "None configured")}</p>
            </div>
          </article>
          <article class="admin-list-row">
            <div>
              <strong>Trusted proxy CIDRs</strong>
              <p class="hint">${window.escapeAdminHtml((payload.trusted_proxy_cidrs || []).join(", ") || "None configured")}</p>
            </div>
          </article>
        </div>
      </section>
    `;
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
