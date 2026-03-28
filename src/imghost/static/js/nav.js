const navRoot = document.querySelector("[data-nav-root]");
const navToggle = document.querySelector("[data-nav-toggle]");
const navMenu = document.querySelector("[data-nav-menu]");
const mobileNavQuery = window.matchMedia("(max-width: 640px)");

if (navRoot && navToggle && navMenu) {
  const syncNav = () => {
    const isMobile = mobileNavQuery.matches;
    const isOpen = navRoot.dataset.navOpen === "true";

    navRoot.dataset.navReady = "true";
    navToggle.hidden = !isMobile;
    navToggle.setAttribute("aria-expanded", String(isMobile && isOpen));
    navToggle.setAttribute("aria-label", isOpen ? "Close navigation menu" : "Open navigation menu");
    navToggle.textContent = isOpen ? "Close" : "Menu";
  };

  const closeNav = () => {
    navRoot.dataset.navOpen = "false";
    syncNav();
  };

  navToggle.addEventListener("click", () => {
    navRoot.dataset.navOpen = navRoot.dataset.navOpen === "true" ? "false" : "true";
    syncNav();
  });

  mobileNavQuery.addEventListener("change", () => {
    if (!mobileNavQuery.matches) {
      closeNav();
      return;
    }
    syncNav();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && navRoot.dataset.navOpen === "true") {
      closeNav();
    }
  });

  document.addEventListener("click", (event) => {
    if (!mobileNavQuery.matches || navRoot.dataset.navOpen !== "true") {
      return;
    }
    if (!(event.target instanceof Node) || navRoot.contains(event.target)) {
      return;
    }
    closeNav();
  });

  navMenu.addEventListener("click", (event) => {
    if (!mobileNavQuery.matches || !(event.target instanceof Element)) {
      return;
    }
    if (event.target.closest("a, button")) {
      closeNav();
    }
  });

  syncNav();
}
