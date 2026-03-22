window.parseOptionalNumber = (value) => (value === "" ? null : Number(value));
window.parseOptionalDate = (value) => (value === "" ? null : new Date(value).toISOString());

window.escapeAdminHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

window.adminRequestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

window.setAdminStatus = (node, message = "", tone = "") => {
  if (!node) {
    return;
  }
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
  if (tone) {
    node.dataset.tone = tone;
  } else {
    delete node.dataset.tone;
  }
};

window.adminFormatNumber = (value) => new Intl.NumberFormat().format(Number(value || 0));

window.adminFormatBytes = (value) => {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unitIndex = 0;
  let size = bytes;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const precision = size >= 10 || unitIndex === 0 ? 0 : 1;
  return `${size.toFixed(precision)} ${units[unitIndex]}`;
};

window.adminFormatDateTime = (value) => {
  if (!value) {
    return "Not recorded";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString();
};

window.renderAdminRuntimeCards = (payload) => {
  const hasSeparateWorkerService = payload.redis?.configured && payload.tasks?.mode === "redis";
  const queueDetails = Object.entries(payload.tasks?.queues || {})
    .map(([name, depth]) => `${name}: ${window.adminFormatNumber(depth)}`)
    .join(" · ");

  const entries = [
    {
      label: "Database",
      status: payload.database?.ok ? "Healthy" : "Unavailable",
      tone: payload.database?.ok ? "ok" : "warn",
      hint: "Primary SQL database health check",
    },
    {
      label: "Storage",
      status: payload.storage?.ok ? "Healthy" : "Unavailable",
      tone: payload.storage?.ok ? "ok" : "warn",
      hint: "Object storage connection status",
    },
    {
      label: "Redis",
      status: payload.redis?.configured ? (payload.redis?.reachable ? "Reachable" : "Configured, not reachable") : "Disabled",
      tone: payload.redis?.configured ? (payload.redis?.reachable ? "ok" : "warn") : "neutral",
      hint: `Sessions ${payload.redis?.subsystems?.sessions?.mode || "unknown"}${payload.redis?.session_fail_closed ? " (fail closed)" : " (graceful fallback)"} · Rate limits ${payload.redis?.subsystems?.rate_limits?.mode || "unknown"}`,
    },
    {
      label: "Task queue",
      status: payload.tasks?.mode || "Unknown",
      tone: payload.tasks?.queue_depth > 0 ? "warn" : "ok",
      hint: `Depth ${window.adminFormatNumber(payload.tasks?.queue_depth || 0)}${queueDetails ? ` · ${queueDetails}` : ""}`,
    },
    {
      label: "Worker",
      status: payload.worker?.enabled_in_this_process
        ? "Running in app process"
        : hasSeparateWorkerService
          ? "Separate worker service"
          : "Disabled",
      tone: payload.worker?.enabled_in_this_process || hasSeparateWorkerService
        ? (payload.worker?.last_task_failure ? "warn" : "ok")
        : "neutral",
      hint: payload.worker?.last_task_failure
        ? `Last failure: ${window.escapeAdminHtml(String(payload.worker.last_task_failure))}`
        : `Last started ${window.adminFormatDateTime(payload.worker?.last_started_at)}`,
    },
    {
      label: "Public origin mode",
      status: payload.public_origin_mode || "Unknown",
      tone: payload.public_origin_enabled ? "ok" : "neutral",
      hint: payload.public_origin_enabled
        ? "Only configured public origins are reflected into links and browser-session CSRF checks."
        : "The app reflects the host the browser used. This is convenient for localhost or LAN testing.",
    },
    {
      label: "Proxy trust",
      status: payload.forwarded_headers_policy || "Unknown",
      tone: payload.trusted_proxy_cidrs_enabled ? "ok" : "warn",
      hint: payload.proxy_trust_warning || `${(payload.trusted_proxy_cidrs || []).length} trusted CIDR(s)`,
    },
  ];

  return `
    <div class="admin-runtime-grid">
      ${entries
        .map(
          (entry) => `
            <article class="admin-runtime-card">
              <div class="admin-status-pill" data-tone="${entry.tone}">${window.escapeAdminHtml(entry.status)}</div>
              <h3>${window.escapeAdminHtml(entry.label)}</h3>
              <p class="hint">${window.escapeAdminHtml(entry.hint)}</p>
            </article>
          `
        )
        .join("")}
    </div>
  `;
};

window.renderAdminNetworkTrust = (payload) => `
  <section class="admin-overview-subsection">
    <div class="admin-section-header">
      <h3>Network trust</h3>
      <p class="hint">Origins and proxy trust that affect request handling.</p>
    </div>
    <div class="item-list">
      <article class="admin-list-row">
        <div>
          <strong>Public origin mode</strong>
          <p class="hint">${window.escapeAdminHtml(payload.public_origin_enabled ? "Strict allowlist mode" : "Direct request mode")}</p>
        </div>
      </article>
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
      <article class="admin-list-row">
        <div>
          <strong>Redis session outage mode</strong>
          <p class="hint">${window.escapeAdminHtml(payload.redis?.session_fail_closed ? "Fail closed: browser sessions require Redis." : "Graceful fallback: signed cookies keep browser auth working if Redis is down.")}</p>
        </div>
      </article>
      ${
        payload.proxy_trust_warning
          ? `
            <article class="admin-list-row">
              <div>
                <strong>Proxy trust note</strong>
                <p class="hint">${window.escapeAdminHtml(payload.proxy_trust_warning)}</p>
              </div>
            </article>
          `
          : ""
      }
    </div>
  </section>
`;
