(() => {
  try {
    const storedTheme = window.localStorage.getItem("imghost-theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const resolvedTheme = storedTheme || (prefersDark ? "dark" : "light");
    document.documentElement.dataset.theme = resolvedTheme;
  } catch {
    document.documentElement.dataset.theme = "light";
  }
})();
