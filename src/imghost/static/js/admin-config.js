const adminConfigForm = document.getElementById("admin-config-form");
const adminConfigJson = document.getElementById("admin-config-json");
const adminConfigStatus = document.getElementById("admin-config-status");

if (adminConfigForm && adminConfigJson) {
  const state = { config: null };

  const renderConfigForm = (config) => {
    adminConfigForm.innerHTML =
      Object.values(config)
        .map((entry) => {
          const isBool = typeof entry.value === "boolean";
          return `
            <label>
              <strong>${window.escapeAdminHtml(entry.key)}</strong> <span class="hint">source=${window.escapeAdminHtml(entry.source)}${entry.locked ? " · locked" : ""}</span>
              ${
                isBool
                  ? `<select name="${entry.key}" ${entry.locked ? "disabled" : ""}>
                       <option value="true" ${entry.value ? "selected" : ""}>true</option>
                       <option value="false" ${!entry.value ? "selected" : ""}>false</option>
                     </select>`
                  : `<input type="number" name="${entry.key}" value="${entry.value}" ${entry.locked ? "disabled" : ""}>`
              }
            </label>
          `;
        })
        .join("") + '<div class="row row-actions"><button type="submit">Save Config</button></div>';
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
      adminConfigJson.textContent = error.message;
    });
  });

  refreshConfig().catch((error) => {
    adminConfigJson.textContent = error.message;
  });
}
