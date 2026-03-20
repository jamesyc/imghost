const authFlash = document.getElementById("flash");

const showAuthMessage = (message) => {
  if (authFlash) {
    authFlash.textContent = message || "";
  }
};

for (const form of document.querySelectorAll("[data-auth-form]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const formData = new FormData(form);
      const payload = Object.fromEntries(formData.entries());
      delete payload.next;
      payload.remember_me = formData.get("remember_me") === "on";
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Request failed.");
      }
      window.location.assign(form.dataset.successUrl || "/dashboard");
    } catch (error) {
      showAuthMessage(error.message);
    }
  });
}
