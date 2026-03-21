const adminCreateUserForm = document.getElementById("admin-create-user-form");
const adminCreateUserStatus = document.getElementById("admin-create-user-status");

if (adminCreateUserForm) {
  adminCreateUserForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    window.setAdminStatus(adminCreateUserStatus);
    try {
      const form = new FormData(event.currentTarget);
      const created = await window.adminRequestJson("/api/v1/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: form.get("username"),
          email: form.get("email"),
          password: form.get("password") || null,
          is_admin: form.get("is_admin") === "on",
          quota_bytes: window.parseOptionalNumber(form.get("quota_bytes")),
          rate_limit_rpm: window.parseOptionalNumber(form.get("rate_limit_rpm")),
          rate_limit_bph: window.parseOptionalNumber(form.get("rate_limit_bph")),
        }),
      });
      event.currentTarget.reset();
      window.setAdminStatus(adminCreateUserStatus, `Admin user created: ${created.username}`, "success");
    } catch (error) {
      window.setAdminStatus(adminCreateUserStatus, error.message, "error");
    }
  });
}
