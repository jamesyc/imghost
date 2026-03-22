const adminRuntimeStatus = document.getElementById("admin-runtime-status");
const adminAuditRoot = document.getElementById("admin-audit");
const adminAuditForm = document.getElementById("admin-audit-form");
const adminAuditStatus = document.getElementById("admin-audit-status");
const adminRuntimeStatusText = document.getElementById("admin-runtime-status-text");
const adminNetworkTrust = document.getElementById("admin-network-trust");
const adminAuditSummary = document.getElementById("admin-audit-summary");
const adminAuditPrev = document.getElementById("admin-audit-prev");
const adminAuditNext = document.getElementById("admin-audit-next");
const adminAuditOffset = document.getElementById("admin-audit-offset");

if (adminRuntimeStatus && adminAuditRoot) {
  const state = {
    lastItems: [],
    lastOffset: 0,
    lastLimit: 25,
  };

  const renderRuntime = (payload) => {
    adminRuntimeStatus.innerHTML = `
      ${window.renderAdminRuntimeCards(payload)}
      ${window.renderAdminRuntimeDetails(payload)}
    `;
    if (adminNetworkTrust) {
      adminNetworkTrust.innerHTML = window.renderAdminNetworkTrust(payload);
    }
  };

  const refreshRuntime = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/runtime-status");
    renderRuntime(payload);
  };

  const buildAuditQuery = (formNode) => {
    const params = new URLSearchParams();
    const form = new FormData(formNode);
    for (const [key, value] of form.entries()) {
      if (value !== "") {
        params.set(key, value);
      }
    }
    if (!params.has("limit")) {
      params.set("limit", "25");
    }
    if (!params.has("offset")) {
      params.set("offset", "0");
    }
    return params;
  };

  const renderAudit = (items) => {
    if (!items.length) {
      adminAuditRoot.innerHTML = '<p class="hint">No audit events matched this query.</p>';
      return;
    }
    adminAuditRoot.innerHTML = items
      .map((entry) => {
        const metadata = entry.metadata && Object.keys(entry.metadata).length
          ? `<details class="admin-debug-toggle"><summary>Metadata</summary><pre class="result">${window.escapeAdminHtml(JSON.stringify(entry.metadata, null, 2))}</pre></details>`
          : "";
        return `
          <article class="admin-audit-entry">
            <div class="admin-audit-entry-header">
              <div>
                <p class="eyebrow">${window.escapeAdminHtml(entry.event_type)}</p>
                <h3>${window.escapeAdminHtml(entry.target_type)} · ${window.escapeAdminHtml(entry.target_id)}</h3>
              </div>
              <p class="hint">${window.adminFormatDateTime(entry.created_at)}</p>
            </div>
            <div class="admin-audit-meta">
              <p><strong>Actor:</strong> ${window.escapeAdminHtml(entry.actor_id || "Anonymous")}</p>
              <p><strong>Correlation:</strong> ${window.escapeAdminHtml(entry.correlation_id)}</p>
              <p><strong>Event ID:</strong> ${window.escapeAdminHtml(entry.id)}</p>
            </div>
            ${metadata}
          </article>
        `;
      })
      .join("");
  };

  const refreshAudit = async (formNode) => {
    const params = buildAuditQuery(formNode);
    const payload = await window.adminRequestJson(`/api/v1/admin/audit?${params.toString()}`);
    state.lastItems = payload;
    state.lastOffset = Number(params.get("offset") || "0");
    state.lastLimit = Number(params.get("limit") || "25");
    renderAudit(payload);
    const start = payload.length ? state.lastOffset + 1 : 0;
    const end = state.lastOffset + payload.length;
    adminAuditSummary.textContent = payload.length
      ? `Showing ${start}-${end} audit event(s).`
      : "No audit events in this range.";
    adminAuditPrev.disabled = state.lastOffset <= 0;
    adminAuditNext.disabled = payload.length < state.lastLimit;
  };

  document.getElementById("refresh-admin-runtime-status")?.addEventListener("click", () => {
    refreshRuntime().catch((error) => {
      window.setAdminStatus(adminRuntimeStatusText, error.message, "error");
    });
  });

  adminAuditForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    window.setAdminStatus(adminAuditStatus);
    try {
      await refreshAudit(event.currentTarget);
      window.setAdminStatus(adminAuditStatus, "Audit log refreshed.", "success");
    } catch (error) {
      window.setAdminStatus(adminAuditStatus, error.message, "error");
    }
  });

  adminAuditPrev?.addEventListener("click", async () => {
    if (!adminAuditForm) {
      return;
    }
    const nextOffset = Math.max(0, state.lastOffset - state.lastLimit);
    adminAuditOffset.value = String(nextOffset);
    try {
      await refreshAudit(adminAuditForm);
    } catch (error) {
      window.setAdminStatus(adminAuditStatus, error.message, "error");
    }
  });

  adminAuditNext?.addEventListener("click", async () => {
    if (!adminAuditForm) {
      return;
    }
    adminAuditOffset.value = String(state.lastOffset + state.lastLimit);
    try {
      await refreshAudit(adminAuditForm);
    } catch (error) {
      window.setAdminStatus(adminAuditStatus, error.message, "error");
    }
  });

  refreshRuntime().catch((error) => {
    window.setAdminStatus(adminRuntimeStatusText, error.message, "error");
  });
  refreshAudit(adminAuditForm).catch((error) => {
    window.setAdminStatus(adminAuditStatus, error.message, "error");
  });
}
