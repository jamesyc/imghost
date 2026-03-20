const authFlash = document.getElementById("flash");

const showAuthMessage = (message) => {
  if (authFlash) {
    authFlash.textContent = message || "";
  }
};

for (const form of document.querySelectorAll("[data-auth-form]")) {
  const passwordInput = form.querySelector('input[name="password"]');
  const confirmPasswordInput = form.querySelector('input[name="confirm_password"]');
  const loginInput = form.querySelector('input[name="login"]');
  const usernameInput = form.querySelector('input[name="username"]');
  const emailInput = form.querySelector('input[name="email"]');
  const inlineError = form.querySelector(".form-error");

  const clearRegisterValidity = () => {
    passwordInput?.setCustomValidity("");
    confirmPasswordInput?.setCustomValidity("");
  };

  const clearInlineError = () => {
    if (inlineError) {
      inlineError.textContent = "";
      inlineError.classList.add("hidden");
    }
  };

  passwordInput?.addEventListener("input", clearRegisterValidity);
  confirmPasswordInput?.addEventListener("input", clearRegisterValidity);
  loginInput?.addEventListener("input", clearInlineError);
  usernameInput?.addEventListener("input", clearInlineError);
  emailInput?.addEventListener("input", clearInlineError);
  passwordInput?.addEventListener("input", clearInlineError);
  confirmPasswordInput?.addEventListener("input", clearInlineError);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      clearInlineError();
      const formData = new FormData(form);
      const password = String(formData.get("password") || "");
      const confirmPassword = String(formData.get("confirm_password") || "");
      if (form.id === "register-form") {
        if (password.length < 8) {
          const message = `Please use at least 8 characters (you are currently using ${password.length} character${password.length === 1 ? "" : "s"}).`;
          passwordInput?.setCustomValidity(message);
          passwordInput?.reportValidity();
          return;
        }
        if (password !== confirmPassword) {
          const message = "Please make sure both passwords match.";
          confirmPasswordInput?.setCustomValidity(message);
          confirmPasswordInput?.reportValidity();
          return;
        }
        clearRegisterValidity();
      }
      const payload = Object.fromEntries(formData.entries());
      delete payload.next;
      delete payload.confirm_password;
      payload.remember_me = formData.get("remember_me") === "on";
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = data.detail || "Request failed.";
        if (form.id === "login-form" && message === "Invalid credentials." && inlineError) {
          inlineError.textContent = message;
          inlineError.classList.remove("hidden");
          return;
        }
        if (form.id === "register-form" && inlineError) {
          inlineError.textContent = message;
          inlineError.classList.remove("hidden");
          return;
        }
        throw new Error(message);
      }
      window.location.assign(form.dataset.successUrl || "/dashboard");
    } catch (error) {
      showAuthMessage(error.message);
    }
  });
}
