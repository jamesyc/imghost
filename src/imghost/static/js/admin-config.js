const adminConfigForm = document.getElementById("admin-config-form");
const adminConfigJson = document.getElementById("admin-config-json");
const adminConfigStatus = document.getElementById("admin-config-status");

if (adminConfigForm && adminConfigJson) {
  const state = { config: null };

  const CONFIG_GROUPS = [
    {
      title: "Access",
      copy: "Control whether people can sign up and whether anonymous uploads stay enabled.",
      keys: ["allow_registration", "anon_upload_enabled", "anon_expiry_hours", "max_upload_bytes", "video_thumb_frames"],
    },
    {
      title: "Storage",
      copy: "Adjust default per-user quota and the whole-server storage ceiling.",
      keys: ["default_user_quota_bytes", "server_quota_bytes"],
    },
    {
      title: "Anonymous limits",
      copy: "Per-request and global limits that apply before a user signs in.",
      keys: ["rate_limit_anon_rpm", "rate_limit_anon_bph", "rate_limit_global_anon_rpm", "rate_limit_global_anon_bph"],
    },
    {
      title: "Signed-in limits",
      copy: "Upload ceilings for authenticated users.",
      keys: ["rate_limit_user_rpm", "rate_limit_user_bph"],
    },
  ];

  const LABELS = {
    allow_registration: "Allow registration",
    anon_upload_enabled: "Allow anonymous uploads",
    anon_expiry_hours: "Anonymous upload expiry hours",
    max_upload_bytes: "Max upload bytes per file",
    video_thumb_frames: "Video thumbnail frames",
    default_user_quota_bytes: "Default user quota bytes",
    server_quota_bytes: "Server quota bytes",
    rate_limit_anon_rpm: "Anonymous requests per minute",
    rate_limit_anon_bph: "Anonymous bytes per hour",
    rate_limit_global_anon_rpm: "Global anonymous requests per minute",
    rate_limit_global_anon_bph: "Global anonymous bytes per hour",
    rate_limit_user_rpm: "Signed-in requests per minute",
    rate_limit_user_bph: "Signed-in bytes per hour",
  };

  const DESCRIPTIONS = {
    allow_registration: "Lets new people create accounts from the public site.",
    anon_upload_enabled: "Keeps upload access available before someone signs in.",
    anon_expiry_hours: "How long anonymous uploads stay online before expiry.",
    max_upload_bytes: "Per-file upload limit enforced for both anonymous and signed-in uploads.",
    video_thumb_frames: "How many frames animated video thumbnails sample before the preview loops.",
    default_user_quota_bytes: "Default storage quota applied when a user does not have an explicit override.",
    server_quota_bytes: "Whole-server storage ceiling. Set to 0 for unlimited.",
    rate_limit_anon_rpm: "Per-client request rate limit for anonymous uploads.",
    rate_limit_anon_bph: "Per-client upload bandwidth budget for anonymous uploads.",
    rate_limit_global_anon_rpm: "Whole-site request cap for all anonymous upload traffic combined.",
    rate_limit_global_anon_bph: "Whole-site bandwidth cap for all anonymous upload traffic combined.",
    rate_limit_user_rpm: "Per-user request rate limit after sign-in.",
    rate_limit_user_bph: "Per-user upload bandwidth budget after sign-in.",
  };

  const formatValue = (entry) => {
    if (typeof entry.value === "boolean") {
      return entry.value ? "Enabled" : "Disabled";
    }
    return entry.key.includes("_bph") || entry.key.includes("_bytes")
      ? window.adminFormatBytes(entry.value)
      : window.adminFormatNumber(entry.value);
  };

  const renderField = (entry) => {
    const isBool = typeof entry.value === "boolean";
    return `
      <label class="admin-config-field">
        <div class="admin-config-field-copy">
          <strong>${window.escapeAdminHtml(LABELS[entry.key] || entry.key)}</strong>
          <p class="hint">${window.escapeAdminHtml(DESCRIPTIONS[entry.key] || "")}</p>
          <p class="hint">Current value: ${window.escapeAdminHtml(formatValue(entry))} · source ${window.escapeAdminHtml(entry.source)}${entry.locked ? " · locked by environment" : ""}</p>
        </div>
        ${
          isBool
            ? `<select name="${entry.key}" ${entry.locked ? "disabled" : ""}>
                 <option value="true" ${entry.value ? "selected" : ""}>Enabled</option>
                 <option value="false" ${!entry.value ? "selected" : ""}>Disabled</option>
               </select>`
            : `<input type="number" min="0" name="${entry.key}" value="${entry.value}" ${entry.locked ? "disabled" : ""}>`
        }
      </label>
    `;
  };

  const renderConfigForm = (config) => {
    adminConfigForm.innerHTML =
      CONFIG_GROUPS.map((group) => {
        const entries = group.keys.map((key) => config[key]).filter(Boolean);
        return `
          <section class="card admin-card admin-config-group">
            <div class="admin-section-header">
              <h3>${window.escapeAdminHtml(group.title)}</h3>
              <p class="hint">${window.escapeAdminHtml(group.copy)}</p>
            </div>
            <div class="stack">
              ${entries.map((entry) => renderField(entry)).join("")}
            </div>
          </section>
        `;
      }).join("") + '<div class="row row-actions"><button type="submit">Save Config</button></div>';
  };

  const refreshConfig = async () => {
    state.config = await window.adminRequestJson("/api/v1/admin/config");
    renderConfigForm(state.config);
    adminConfigJson.textContent = JSON.stringify(state.config, null, 2);
  };

  adminConfigForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    window.setAdminStatus(adminConfigStatus);
    try {
      const form = new FormData(event.currentTarget);
      const payload = {};
      for (const [key, value] of form.entries()) {
        const current = state.config[key];
        payload[key] = typeof current.value === "boolean" ? value === "true" : Number(value);
      }
      await window.adminRequestJson("/api/v1/admin/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await refreshConfig();
      window.setAdminStatus(adminConfigStatus, "Runtime config updated.", "success");
    } catch (error) {
      window.setAdminStatus(adminConfigStatus, error.message, "error");
    }
  });

  document.getElementById("refresh-admin-config")?.addEventListener("click", () => {
    refreshConfig().catch((error) => {
      window.setAdminStatus(adminConfigStatus, error.message, "error");
    });
  });

  refreshConfig().catch((error) => {
    window.setAdminStatus(adminConfigStatus, error.message, "error");
  });
}
