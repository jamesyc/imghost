(function () {
  const storageKey = "imghost-anon-albums";

  const readMap = () => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
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

  const deleteTokenFromDeleteUrl = (deleteUrl) => {
    if (!deleteUrl) {
      return "";
    }
    try {
      return new URL(deleteUrl, window.location.origin).searchParams.get("delete_token") || "";
    } catch {
      return "";
    }
  };

  window.imghostAnonAlbums = {
    deleteTokenFromDeleteUrl,
    manageUrl,
    remember({ albumId, deleteToken }) {
      if (!albumId || !deleteToken) {
        return "";
      }
      const next = readMap();
      next[albumId] = {
        deleteToken,
        savedAt: new Date().toISOString(),
      };
      writeMap(next);
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
    },
  };
})();
