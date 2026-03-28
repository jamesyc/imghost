const canRegisterServiceWorker = (() => {
  if (!("serviceWorker" in navigator)) {
    return false;
  }

  if (window.isSecureContext) {
    return true;
  }

  const { hostname } = window.location;
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
})();

if (canRegisterServiceWorker) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {
      // Home-screen support should fail quietly on unsupported/self-hosted setups.
    });
  });
}
