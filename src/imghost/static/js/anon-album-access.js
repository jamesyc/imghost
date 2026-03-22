(function () {
  const storageKey = "imghost-anon-albums";
  const manageCookiePrefix = "imghost_manage_";
  const maxStoredAlbums = 500;
  const maxAgeMs = 7 * 24 * 60 * 60 * 1000;

  const isoTimestamp = () => new Date().toISOString();
  const isFreshRecord = (record, now) => {
    if (!record?.deleteToken || typeof record.deleteToken !== "string") {
      return false;
    }
    const savedAtMs = Date.parse(record.savedAt || "");
    return Number.isFinite(savedAtMs) && now - savedAtMs <= maxAgeMs;
  };

  const pruneMap = (value) => {
    const now = Date.now();
    const entries = Object.entries(value || {})
      .filter(([, record]) => isFreshRecord(record, now))
      .sort((a, b) => Date.parse(b[1].savedAt || "") - Date.parse(a[1].savedAt || ""));

    return Object.fromEntries(entries.slice(0, maxStoredAlbums));
  };

  const cookieNameForAlbum = (albumId) => `${manageCookiePrefix}${albumId}`;
  const cookiePathForAlbum = (albumId) => `/manage/${encodeURIComponent(albumId)}`;

  const setManageCookie = (albumId, deleteToken) => {
    if (!albumId || !deleteToken) {
      return;
    }
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${cookieNameForAlbum(albumId)}=${encodeURIComponent(deleteToken)}; Path=${cookiePathForAlbum(albumId)}; Max-Age=${Math.floor(maxAgeMs / 1000)}; SameSite=Lax${secure}`;
  };

  const clearManageCookie = (albumId) => {
    if (!albumId) {
      return;
    }
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${cookieNameForAlbum(albumId)}=; Path=${cookiePathForAlbum(albumId)}; Max-Age=0; SameSite=Lax${secure}`;
  };

  const readMap = () => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      const parsed = raw ? JSON.parse(raw) : {};
      const normalized = parsed && typeof parsed === "object" ? pruneMap(parsed) : {};
      if (raw && JSON.stringify(normalized) !== raw) {
        writeMap(normalized);
      }
      return normalized;
    } catch {
      return {};
    }
  };

  const writeMap = (value) => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(value));
    } catch {
      // ignore storage failures
    }
  };

  const manageUrl = (albumId, deleteToken) => `/manage/${encodeURIComponent(albumId)}?token=${encodeURIComponent(deleteToken)}`;

  const deleteTokenFromManageUrl = (manageUrl) => {
    if (!manageUrl) {
      return "";
    }
    try {
      return new URL(manageUrl, window.location.origin).searchParams.get("token") || "";
    } catch {
      return "";
    }
  };

  window.imghostAnonAlbums = {
    deleteTokenFromManageUrl,
    manageUrl,
    remember({ albumId, deleteToken }) {
      if (!albumId || !deleteToken) {
        return "";
      }
      const next = pruneMap(readMap());
      next[albumId] = {
        deleteToken,
        savedAt: isoTimestamp(),
      };
      const pruned = pruneMap(next);
      writeMap(pruned);
      setManageCookie(albumId, deleteToken);
      return manageUrl(albumId, deleteToken);
    },
    read(albumId) {
      const record = readMap()[albumId];
      if (!record?.deleteToken) {
        return null;
      }
      return {
        albumId,
        deleteToken: record.deleteToken,
        manageUrl: manageUrl(albumId, record.deleteToken),
      };
    },
    forget(albumId) {
      if (!albumId) {
        return;
      }
      const next = readMap();
      delete next[albumId];
      writeMap(next);
      clearManageCookie(albumId);
    },
  };
})();
