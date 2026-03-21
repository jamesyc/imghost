const adminOverviewStats = document.getElementById("admin-overview-stats");
const adminOverviewRuntime = document.getElementById("admin-overview-runtime");

if (adminOverviewStats && adminOverviewRuntime) {
  const refreshStats = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/stats");
    adminOverviewStats.textContent = JSON.stringify(payload, null, 2);
  };

  const refreshRuntime = async () => {
    const payload = await window.adminRequestJson("/api/v1/admin/runtime-status");
    adminOverviewRuntime.textContent = JSON.stringify(payload, null, 2);
  };

  document.getElementById("admin-overview-refresh-stats")?.addEventListener("click", () => {
    refreshStats().catch((error) => {
      adminOverviewStats.textContent = error.message;
    });
  });

  document.getElementById("admin-overview-refresh-runtime")?.addEventListener("click", () => {
    refreshRuntime().catch((error) => {
      adminOverviewRuntime.textContent = error.message;
    });
  });

  refreshStats().catch((error) => {
    adminOverviewStats.textContent = error.message;
  });
  refreshRuntime().catch((error) => {
    adminOverviewRuntime.textContent = error.message;
  });
}
