const adminRuntimeStatus = document.getElementById("admin-runtime-status");
const adminAuditRoot = document.getElementById("admin-audit");
const adminAuditForm = document.getElementById("admin-audit-form");
const adminAuditStatus = document.getElementById("admin-audit-status");

if (adminRuntimeStatus && adminAuditRoot) {
  const refreshRuntime = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/runtime-status");
    adminRuntimeStatus.textContent = JSON.stringify(payload, null, 2);
  };

  const refreshAudit = async (formNode = null) => {
    const params = new URLSearchParams();
    if (formNode) {
      const form = new FormData(formNode);
      for (const [key, value] of form.entries()) {
        if (value !== "") {
          params.set(key, value);
        }
      }
    } else {
      params.set("limit", "100");
    }
    const payload = await window.adminRequestJson(`/api/v1/admin/audit?${params.toString()}`);
    adminAuditRoot.textContent = JSON.stringify(payload, null, 2);
  };

  document.getElementById("refresh-admin-runtime-status")?.addEventListener("click", () => {
    refreshRuntime().catch((error) => {
      adminRuntimeStatus.textContent = error.message;
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

  refreshRuntime().catch((error) => {
    adminRuntimeStatus.textContent = error.message;
  });
  refreshAudit().catch((error) => {
    adminAuditRoot.textContent = error.message;
  });
}
