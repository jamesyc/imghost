const adminUsersRoot = document.getElementById("admin-users");
const adminUsersSearchForm = document.getElementById("admin-user-search-form");
const adminUsersSummary = document.getElementById("admin-users-summary");
const adminUsersPrev = document.getElementById("admin-users-prev");
const adminUsersNext = document.getElementById("admin-users-next");

if (adminUsersRoot) {
  const state = {
    q: "",
    is_admin: "",
    suspended: "",
    limit: 50,
    offset: 0,
    total: 0,
  };

  const buildParams = () => {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.is_admin) params.set("is_admin", state.is_admin);
    if (state.suspended) params.set("suspended", state.suspended);
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params;
  };

  const renderUsers = (payload) => {
    state.total = payload.total;
    adminUsersRoot.innerHTML = payload.items
      .map(
        (user) => `
        <section class="admin-card" data-user-id="${user.id}">
          <div class="admin-record-header">
            <div>
              <h3>${window.escapeAdminHtml(user.username)}${user.is_admin ? " (admin)" : ""}</h3>
              <p class="hint">${window.escapeAdminHtml(user.email)} · suspended=${user.suspended} · storage=${user.storage_used_bytes} · media=${user.media_count}</p>
            </div>
          </div>
          <form class="admin-user-patch-form stack">
            <label class="check"><input type="checkbox" name="suspended" ${user.suspended ? "checked" : ""}> Suspended</label>
            <input type="number" name="quota_bytes" placeholder="Quota bytes" value="${user.quota_bytes ?? ""}">
            <input type="number" name="rate_limit_rpm" placeholder="Requests per minute override" value="${user.rate_limit_rpm ?? ""}">
            <input type="number" name="rate_limit_bph" placeholder="Bytes per hour override" value="${user.rate_limit_bph ?? ""}">
            <div class="row row-actions">
              <button type="submit">Patch User</button>
            </div>
          </form>
          <form class="admin-user-reset-form stack">
            <input type="password" name="new_password" placeholder="New password" required>
            <div class="row row-actions">
              <button type="submit" class="secondary">Reset Password</button>
            </div>
          </form>
          <div class="row row-actions admin-delete-row">
            <button type="button" class="danger admin-user-delete">Delete User</button>
          </div>
          <p class="settings-inline-status hidden admin-item-status" aria-live="polite"></p>
        </section>
      `,
      )
      .join("");

    const start = payload.total === 0 ? 0 : payload.offset + 1;
    const end = payload.offset + payload.items.length;
    adminUsersSummary.textContent = payload.total
      ? `Showing ${start}-${end} of ${payload.total} users`
      : "No users found.";
    adminUsersPrev.disabled = payload.offset <= 0;
    adminUsersNext.disabled = !payload.has_more;
  };

  const refreshUsers = async () => {
    const payload = await window.adminRequestJson(`/api/v1/admin/users?${buildParams().toString()}`);
    renderUsers(payload);
  };

  adminUsersSearchForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    state.q = String(form.get("q") || "").trim();
    state.is_admin = String(form.get("is_admin") || "");
    state.suspended = String(form.get("suspended") || "");
    state.offset = 0;
    try {
      await refreshUsers();
    } catch (error) {
      adminUsersRoot.textContent = error.message;
    }
  });

  adminUsersRoot.addEventListener("submit", async (event) => {
    const card = event.target.closest("[data-user-id]");
    if (!card) return;
    event.preventDefault();
    const status = card.querySelector(".admin-item-status");
    window.setAdminStatus(status);
    const userId = card.dataset.userId;
    try {
      if (event.target.matches(".admin-user-patch-form")) {
        const form = new FormData(event.target);
        await window.adminRequestJson(`/api/v1/admin/users/${userId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            suspended: form.get("suspended") === "on",
            quota_bytes: window.parseOptionalNumber(form.get("quota_bytes")),
            rate_limit_rpm: window.parseOptionalNumber(form.get("rate_limit_rpm")),
            rate_limit_bph: window.parseOptionalNumber(form.get("rate_limit_bph")),
          }),
        });
        window.setAdminStatus(status, "User updated.", "success");
      } else if (event.target.matches(".admin-user-reset-form")) {
        const form = new FormData(event.target);
        await window.adminRequestJson(`/api/v1/admin/users/${userId}/reset-password`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_password: form.get("new_password") }),
        });
        window.setAdminStatus(status, "Password reset.", "success");
      }
      await refreshUsers();
    } catch (error) {
      window.setAdminStatus(status, error.message, "error");
    }
  });

  adminUsersRoot.addEventListener("click", async (event) => {
    const card = event.target.closest("[data-user-id]");
    if (!card || !event.target.matches(".admin-user-delete")) return;
    if (!window.confirm(`Delete user ${card.dataset.userId}?`)) return;
    const status = card.querySelector(".admin-item-status");
    window.setAdminStatus(status);
    try {
      await window.adminRequestJson(`/api/v1/admin/users/${card.dataset.userId}`, { method: "DELETE" });
      await refreshUsers();
    } catch (error) {
      window.setAdminStatus(status, error.message, "error");
    }
  });

  adminUsersPrev?.addEventListener("click", async () => {
    state.offset = Math.max(0, state.offset - state.limit);
    try {
      await refreshUsers();
    } catch (error) {
      adminUsersRoot.textContent = error.message;
    }
  });

  adminUsersNext?.addEventListener("click", async () => {
    state.offset += state.limit;
    try {
      await refreshUsers();
    } catch (error) {
      adminUsersRoot.textContent = error.message;
    }
  });

  document.getElementById("refresh-admin-users")?.addEventListener("click", () => {
    refreshUsers().catch((error) => {
      adminUsersRoot.textContent = error.message;
    });
  });

  refreshUsers().catch((error) => {
    adminUsersRoot.textContent = error.message;
  });
}
