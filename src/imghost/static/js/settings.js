const settingsBootstrapNode = document.getElementById("settings-bootstrap");

if (settingsBootstrapNode) {
  const bootstrap = JSON.parse(settingsBootstrapNode.textContent || "{}");
  const apiKeyOutput = document.getElementById("settings-api-key-output");
  const apiWarning = document.getElementById("settings-api-warning");
  const passwordStatus = document.getElementById("settings-password-status");
  const deleteStatus = document.getElementById("settings-delete-status");
  const defaultApiWarningText = apiWarning?.textContent || "";

  const state = {
    user: bootstrap.session_user || null,
  };

  const formatNumber = (value) => new Intl.NumberFormat().format(Number(value || 0));

  const formatBytes = (value) => {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unitIndex = 0;
    let size = bytes;
    while (size >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }
    const precision = size >= 10 || unitIndex === 0 ? 0 : 1;
    return `${size.toFixed(precision)} ${units[unitIndex]}`;
  };

  const setInlineStatus = (node, message = "", tone = "") => {
    if (!node) {
      return;
    }
    node.textContent = message || "";
    node.classList.toggle("hidden", !message);
    if (tone) {
      node.dataset.tone = tone;
    } else {
      delete node.dataset.tone;
    }
  };

  const revealApiWarning = () => {
    apiWarning?.classList.remove("hidden");
  };

  const setApiWarning = (message, tone = "") => {
    revealApiWarning();
    if (apiWarning) {
      apiWarning.textContent = message || defaultApiWarningText;
      if (tone) {
        apiWarning.dataset.tone = tone;
      } else {
        delete apiWarning.dataset.tone;
      }
    }
  };

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || `Request failed (${response.status}).`);
    }
    return data;
  };

  const triggerDownload = (payload) => {
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "imghost.sxcu";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  const renderUser = () => {
    const user = state.user;
    if (!user) {
      return;
    }
    document.getElementById("settings-username").textContent = user.username || "-";
    document.getElementById("settings-email").textContent = user.email || "-";
    document.getElementById("settings-album-count").textContent = formatNumber(user.album_count);
    document.getElementById("settings-media-count").textContent = formatNumber(user.media_count);
    document.getElementById("settings-storage-used").textContent = formatBytes(user.storage_used_bytes);
    document.getElementById("settings-storage-quota").textContent = formatBytes(user.quota_bytes);
  };

  const refreshUser = async () => {
    state.user = await requestJson("/api/v1/user/me");
    renderUser();
  };

  const rotateAndRevealApiKey = async (message) => {
    setApiWarning(defaultApiWarningText);
    const issued = await requestJson("/api/v1/user/me/api-key", { method: "POST" });
    apiKeyOutput.classList.remove("hidden");
    apiKeyOutput.textContent = JSON.stringify(issued, null, 2);
    await refreshUser();
    setApiWarning(`${message} Save or copy your new key right away. This is the only time you will see it.`, "success");
  };

  document.getElementById("reveal-api-key")?.addEventListener("click", async () => {
    try {
      await rotateAndRevealApiKey("API key rotated and revealed.");
    } catch (error) {
      setApiWarning(error.message, "error");
    }
  });

  document.getElementById("download-sharex-settings")?.addEventListener("click", async () => {
    try {
      setApiWarning(defaultApiWarningText);
      const data = await requestJson("/api/v1/user/me/sharex-config");
      triggerDownload(data);
      await refreshUser();
      setApiWarning("ShareX config downloaded. Browser-session download rotates the API key before embedding it.", "success");
    } catch (error) {
      setApiWarning(error.message, "error");
    }
  });

  document.getElementById("settings-password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      setInlineStatus(passwordStatus);
      const formData = new FormData(event.currentTarget);
      const newPassword = String(formData.get("new_password") || "");
      const confirmPassword = String(formData.get("confirm_new_password") || "");
      if (newPassword !== confirmPassword) {
        setInlineStatus(passwordStatus, "New passwords do not match.", "error");
        return;
      }
      await requestJson("/api/v1/user/me/password", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: String(formData.get("current_password") || ""),
          new_password: newPassword,
        }),
      });
      event.currentTarget.reset();
      setInlineStatus(passwordStatus, "Password changed.", "success");
    } catch (error) {
      setInlineStatus(passwordStatus, error.message, "error");
    }
  });

  document.getElementById("settings-delete-account-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setInlineStatus(deleteStatus);
    if (!window.confirm("Delete your account and all owned content?")) {
      return;
    }
    try {
      await requestJson("/api/v1/user/me", { method: "DELETE" });
      window.location.href = "/";
    } catch (error) {
      setInlineStatus(deleteStatus, error.message, "error");
    }
  });

  renderUser();
  if (state.user && !state.user.has_api_key) {
    rotateAndRevealApiKey("No API key existed, so one was issued automatically.").catch((error) => {
      setApiWarning(error.message, "error");
    });
  }
}
