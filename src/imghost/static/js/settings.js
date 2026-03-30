const settingsBootstrapNode = document.getElementById("settings-bootstrap");

if (settingsBootstrapNode) {
  const bootstrap = JSON.parse(settingsBootstrapNode.textContent || "{}");
  const apiKeyOutput = document.getElementById("settings-api-key-output");
  const apiWarning = document.getElementById("settings-api-warning");
  const passwordStatus = document.getElementById("settings-password-status");
  const oauthStatus = document.getElementById("settings-oauth-status");
  const deleteStatus = document.getElementById("settings-delete-status");
  const ssoSummary = document.getElementById("settings-sso-summary");
  const googleConnect = document.getElementById("settings-google-connect");
  const googleDisconnect = document.getElementById("settings-google-disconnect");
  const passwordHint = document.getElementById("settings-password-hint");
  const oauthHint = document.getElementById("settings-oauth-hint");
  const deleteHint = document.getElementById("settings-delete-hint");
  const deletePasswordInput = document.getElementById("settings-delete-current-password");
  const deleteOauthActions = document.getElementById("settings-delete-oauth-actions");
  const storageUsageBar = document.getElementById("settings-storage-usage-bar");
  const storageUsageCopy = document.getElementById("settings-storage-usage-copy");
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

  const formatPercent = (value) => `${Math.round(Math.max(0, Math.min(100, value)))}%`;

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

  const providerDisplayName = (provider) => {
    const normalized = String(provider || "").trim().toLowerCase();
    if (normalized === "google") {
      return "Google";
    }
    if (normalized === "github") {
      return "GitHub";
    }
    return normalized ? normalized.charAt(0).toUpperCase() + normalized.slice(1) : "OAuth";
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
    const usedBytes = Number(user.storage_used_bytes || 0);
    const quotaBytes = Number(user.quota_bytes || 0);
    const usagePercent = quotaBytes > 0 ? (usedBytes / quotaBytes) * 100 : 0;
    if (storageUsageBar) {
      storageUsageBar.style.width = quotaBytes > 0 ? formatPercent(usagePercent) : "0%";
    }
    if (storageUsageCopy) {
      storageUsageCopy.textContent = quotaBytes > 0
        ? `Using ${formatBytes(usedBytes)} of ${formatBytes(quotaBytes)}.`
        : `Using ${formatBytes(usedBytes)} with no quota limit set.`;
    }
    const linkedProviders = Array.isArray(user.sso_providers) ? user.sso_providers : [];
    const hasPassword = !!user.has_password;
    const googleLinked = linkedProviders.some((provider) => provider.provider === "google");
    const onlyGoogleLinked = googleLinked && linkedProviders.length === 1;
    const disconnectBlocked = onlyGoogleLinked && !hasPassword;
    if (ssoSummary) {
      if (linkedProviders.length === 0) {
        ssoSummary.innerHTML = '<p class="hint">No external sign-in providers are connected. You can keep using your local password only, or add Google as an extra sign-in method.</p>';
      } else {
        ssoSummary.innerHTML = linkedProviders
          .map((provider) => `<p class="hint"><strong>${provider.provider}</strong> connected and ready for sign-in.</p>`)
          .join("");
      }
    }
    if (passwordHint) {
      passwordHint.textContent = hasPassword
        ? "Your account has a local password configured. You can still use it if Google sign-in is unavailable."
        : "Set a local password before disconnecting Google, so you do not lose your only sign-in method.";
    }
    if (oauthHint) {
      if (!googleLinked) {
        oauthHint.textContent = "Connect Google only after signing in locally if you are attaching it to an existing account. If the email already belongs to a local account, imghost will not merge it automatically.";
      } else if (disconnectBlocked) {
        oauthHint.textContent = "Google is currently your only sign-in method. Set a local password first, then disconnect Google if you still want to remove it.";
      } else {
        oauthHint.textContent = "Google is connected. You can disconnect it because your account still has another way to sign in.";
      }
    }
    googleConnect?.classList.toggle("hidden", googleLinked);
    googleDisconnect?.classList.toggle("hidden", !googleLinked);
    if (googleDisconnect) {
      googleDisconnect.disabled = disconnectBlocked;
      googleDisconnect.title = disconnectBlocked
        ? "Set a local password before disconnecting Google."
        : "";
    }
    if (deleteHint) {
      if (hasPassword && linkedProviders.length) {
        deleteHint.textContent = "Delete this account by entering your current password, or confirm with a fresh OAuth sign-in from one of your linked providers.";
      } else if (hasPassword) {
        deleteHint.textContent = "Delete this account by entering your current password.";
      } else if (linkedProviders.length) {
        deleteHint.textContent = "Delete this account by confirming with a fresh OAuth sign-in from one of your linked providers.";
      } else {
        deleteHint.textContent = "This account cannot be deleted until it has a usable local password or linked OAuth provider.";
      }
    }
    if (deletePasswordInput) {
      deletePasswordInput.classList.toggle("hidden", !hasPassword);
      if (!hasPassword) {
        deletePasswordInput.value = "";
      }
    }
    if (deleteOauthActions) {
      deleteOauthActions.innerHTML = linkedProviders.length
        ? linkedProviders
            .map((provider) => {
              const providerName = providerDisplayName(provider.provider);
              const href = `/auth/${encodeURIComponent(provider.provider)}/start?mode=delete_account&next=%2Fsettings`;
              return `<a class="button secondary" href="${href}">Re-auth With ${providerName}</a>`;
            })
            .join("")
        : "";
      deleteOauthActions.classList.toggle("hidden", linkedProviders.length === 0);
    }
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
      const hadPassword = !!state.user?.has_password;
      const newPassword = String(formData.get("new_password") || "");
      const confirmPassword = String(formData.get("confirm_new_password") || "");
      const currentPassword = String(formData.get("current_password") || "");
      if (newPassword.length < 8) {
        setInlineStatus(passwordStatus, "Please use at least 8 characters for the new password.", "error");
        return;
      }
      if (state.user?.has_password && !currentPassword) {
        setInlineStatus(passwordStatus, "Please enter your current password.", "error");
        return;
      }
      if (newPassword !== confirmPassword) {
        setInlineStatus(passwordStatus, "New passwords do not match.", "error");
        return;
      }
      await requestJson("/api/v1/user/me/password", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      if (state.user) {
        state.user.has_password = true;
      }
      event.currentTarget.reset();
      renderUser();
      setInlineStatus(passwordStatus, hadPassword ? "Password changed." : "Password set.", "success");
    } catch (error) {
      setInlineStatus(passwordStatus, error.message, "error");
    }
  });

  googleDisconnect?.addEventListener("click", async () => {
    try {
      setInlineStatus(oauthStatus);
      const payload = await requestJson("/api/v1/user/me/oauth/google/disconnect", { method: "POST" });
      state.user = payload.user;
      renderUser();
      setInlineStatus(oauthStatus, "Google account disconnected.", "success");
    } catch (error) {
      setInlineStatus(oauthStatus, error.message, "error");
    }
  });

  document.getElementById("settings-delete-account-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    setInlineStatus(deleteStatus);
    if (!window.confirm("Delete your account and all owned content?")) {
      return;
    }
    try {
      const currentPassword = String(deletePasswordInput?.value || "");
      const payload = currentPassword
        ? { method: "password", current_password: currentPassword }
        : { method: "oauth_reauth" };
      if (currentPassword) {
        deletePasswordInput.value = "";
      }
      await requestJson("/api/v1/user/me", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      window.location.href = "/";
    } catch (error) {
      setInlineStatus(deleteStatus, error.message, "error");
    }
  });

  if (bootstrap.delete_reauth?.status) {
    setInlineStatus(deleteStatus, bootstrap.delete_reauth.status, bootstrap.delete_reauth.tone || "");
  }
  renderUser();
}
