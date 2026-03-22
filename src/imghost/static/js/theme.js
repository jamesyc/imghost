const root = document.documentElement;
const themeToggle = document.querySelector("[data-theme-toggle]");
const themeIcon = document.querySelector("[data-theme-icon]");
const storageKey = "imghost-theme";

const applyTheme = (theme) => {
  root.dataset.theme = theme;
  if (themeIcon) {
    themeIcon.textContent = theme === "dark" ? "☀" : "☾";
  }
  if (themeToggle) {
    themeToggle.setAttribute("aria-pressed", String(theme === "dark"));
    themeToggle.setAttribute("title", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
  }
};

applyTheme(root.dataset.theme || "light");

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) {
    return;
  }
  if (!target.classList.contains("split-link-value")) {
    return;
  }
  target.select();
});

themeToggle?.addEventListener("click", () => {
  const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
  window.localStorage.setItem(storageKey, nextTheme);
  applyTheme(nextTheme);
});

document.querySelector("[data-logout-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await fetch("/api/v1/auth/logout", { method: "POST" });
  window.location.reload();
});
