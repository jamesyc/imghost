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

window.adminFormatForwardedHeadersPolicy = (value) => {
  if (value === "trusted_proxies_only") {
    return "Trusted proxies only";
  }
  if (value === "permissive") {
    return "Permissive local mode";
  }
  return value || "Unknown";
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
        ? "Strict mode. Generated links, OAuth callbacks, ShareX config, and browser-session CSRF checks only trust configured public origins."
        : "Direct-request mode. The app reflects the host the browser used, which is convenient for localhost and LAN deployments.",
    },
    {
      label: "Proxy trust",
      status: window.adminFormatForwardedHeadersPolicy(payload.forwarded_headers_policy),
      tone: payload.trusted_proxy_cidrs_enabled ? "ok" : "warn",
      hint: payload.proxy_trust_warning
        || (payload.trusted_proxy_cidrs_enabled
          ? `${(payload.trusted_proxy_cidrs || []).length} trusted CIDR(s). Only those peers may set forwarded host/proto.`
          : "Forwarded headers are accepted from any client. Fine for local use, but tighten this behind a real reverse proxy."),
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
  <div class="item-list">
    <article class="admin-list-row">
      <div>
        <strong>Public origin mode</strong>
        <p class="hint">${window.escapeAdminHtml(payload.public_origin_enabled ? "Strict allowlist mode for real deployments" : "Direct request mode for localhost or LAN use")}</p>
      </div>
    </article>
    <article class="admin-list-row">
      <div>
        <strong>Trusted public origins</strong>
        <p class="hint">${window.escapeAdminHtml((payload.trusted_public_origins || []).join(", ") || "None configured")}</p>
        <p class="hint">These are the only browser-visible origins reflected into links and browser-session checks when strict mode is enabled.</p>
      </div>
    </article>
    <article class="admin-list-row">
      <div>
        <strong>Trusted proxy CIDRs</strong>
        <p class="hint">${window.escapeAdminHtml((payload.trusted_proxy_cidrs || []).join(", ") || "None configured")}</p>
        <p class="hint">Enable this when nginx, Caddy, Traefik, or Cloudflare sits in front of imghost so only that proxy can set forwarded host and protocol.</p>
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
`;

window.renderAdminRuntimeDetails = (payload) => {
  const queueRows = Object.entries(payload.tasks?.queues || {})
    .map(
      ([name, depth]) => `
        <article class="admin-list-row">
          <div>
            <strong>Queue ${window.escapeAdminHtml(name)}</strong>
            <p class="hint">Depth ${window.escapeAdminHtml(window.adminFormatNumber(depth))}</p>
          </div>
        </article>
      `
    )
    .join("");

  const subsystemRows = Object.entries(payload.redis?.subsystems || {})
    .map(
      ([name, subsystem]) => `
        <article class="admin-list-row">
          <div>
            <strong>Redis ${window.escapeAdminHtml(name)}</strong>
            <p class="hint">
              configured=${window.escapeAdminHtml(String(Boolean(subsystem?.configured)))} ·
              reachable=${window.escapeAdminHtml(String(Boolean(subsystem?.reachable)))} ·
              mode=${window.escapeAdminHtml(subsystem?.effective_mode || subsystem?.mode || "unknown")} ·
              degraded=${window.escapeAdminHtml(String(Boolean(subsystem?.degraded)))}
            </p>
            <p class="hint">
              last operation: ${window.escapeAdminHtml(subsystem?.last_operation || "Not recorded")} ·
              last error: ${window.escapeAdminHtml(subsystem?.last_error || "Not recorded")}
            </p>
            <p class="hint">
              degraded at: ${window.escapeAdminHtml(window.adminFormatDateTime(subsystem?.last_degraded_at))} ·
              recovered at: ${window.escapeAdminHtml(window.adminFormatDateTime(subsystem?.last_recovered_at))}
            </p>
          </div>
        </article>
      `
    )
    .join("");

  return `
    <div class="item-list">
      <article class="admin-list-row">
        <div>
          <strong>Worker</strong>
          <p class="hint">
            enabled in this process=${window.escapeAdminHtml(String(Boolean(payload.worker?.enabled_in_this_process)))} ·
            last started=${window.escapeAdminHtml(window.adminFormatDateTime(payload.worker?.last_started_at))} ·
            last stopped=${window.escapeAdminHtml(window.adminFormatDateTime(payload.worker?.last_stopped_at))}
          </p>
          <p class="hint">
            last task failure at=${window.escapeAdminHtml(window.adminFormatDateTime(payload.worker?.last_task_failure_at))} ·
            last task failure=${window.escapeAdminHtml(payload.worker?.last_task_failure ? JSON.stringify(payload.worker.last_task_failure) : "Not recorded")}
          </p>
        </div>
      </article>
      <article class="admin-list-row">
        <div>
          <strong>Tasks</strong>
          <p class="hint">
            mode=${window.escapeAdminHtml(payload.tasks?.mode || "unknown")} ·
            backend=${window.escapeAdminHtml(payload.tasks?.queue_backend || "unknown")} ·
            queue depth=${window.escapeAdminHtml(window.adminFormatNumber(payload.tasks?.queue_depth || 0))}
          </p>
          <p class="hint">
            worker count=${window.escapeAdminHtml(window.adminFormatNumber(payload.tasks?.worker_count || 0))} ·
            active workers=${window.escapeAdminHtml(window.adminFormatNumber(payload.tasks?.active_workers || 0))} ·
            active jobs=${window.escapeAdminHtml(window.adminFormatNumber(payload.tasks?.active_jobs || 0))}
          </p>
        </div>
      </article>
      ${queueRows}
      ${subsystemRows}
    </div>
  `;
};
