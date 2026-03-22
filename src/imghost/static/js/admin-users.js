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
              <h3><a href="/admin/users/${user.id}">${window.escapeAdminHtml(user.username)}</a>${user.is_admin ? " (admin)" : ""}</h3>
              <p class="hint">${window.escapeAdminHtml(user.email)} · suspended=${user.suspended} · storage=${user.storage_used_bytes} · media=${user.media_count}</p>
            </div>
            <div class="row row-actions">
              <a class="button-link secondary-link" href="/admin/users/${user.id}">Open</a>
            </div>
          </div>
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
