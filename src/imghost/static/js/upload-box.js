window.resolveUploadDestination = ({ albumId, albumUrl, isAuthenticated }) => {
  if (albumId && isAuthenticated) {
    return `/albums/${encodeURIComponent(albumId)}`;
  }
  if (albumUrl) {
    return albumUrl;
  }
  if (albumId) {
    return `/a/${encodeURIComponent(albumId)}`;
  }
  return "";
};

window.createUploadStatusController = (uploadForm) => {
  const summaryNode = uploadForm?.querySelector(".upload-file-summary");
  const storageKey = uploadForm?.id ? `imghost-upload-status:${uploadForm.id}` : "";

  const setStatus = (message, tone = "") => {
    if (!summaryNode) {
      return;
    }
    summaryNode.textContent = message || "";
    summaryNode.dataset.tone = tone || "";
  };

  const clearStatus = () => {
    if (!summaryNode) {
      return;
    }
    summaryNode.textContent = "";
    delete summaryNode.dataset.tone;
  };

  const readStoredStatus = () => {
    if (!storageKey) {
      return null;
    }
    try {
      const raw = window.sessionStorage.getItem(storageKey);
      if (!raw) {
        return null;
      }
      window.sessionStorage.removeItem(storageKey);
      return JSON.parse(raw);
    } catch {
      return null;
    }
  };

  const storeStatus = (message, tone = "") => {
    if (!storageKey) {
      return;
    }
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify({ message, tone }));
    } catch {
      // ignore storage failures
    }
  };

  const syncSelection = (input) => {
    if (!summaryNode) {
      return;
    }
    const fileCount = input?.files?.length || 0;
    if (!fileCount) {
      clearStatus();
      return;
    }
      setStatus(
      fileCount === 1 ? input.files[0].name : `${fileCount} files selected`,
      "",
    );
  };

  const restoreStoredStatus = () => {
    const stored = readStoredStatus();
    if (stored?.message) {
      setStatus(stored.message, stored.tone || "");
      return true;
    }
    return false;
  };

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      if (!restoreStoredStatus()) {
        clearStatus();
        syncSelection(uploadForm?.querySelector('input[type="file"][name="file"]'));
      }
    }
  });

  restoreStoredStatus();

  return {
    clearStatus,
    persistSuccess(message) {
      storeStatus(message, "success");
      setStatus(message, "success");
    },
    setError(message) {
      setStatus(message, "error");
    },
    setInfo(message) {
      setStatus(message, "");
    },
    setSuccess(message) {
      setStatus(message, "success");
    },
    syncSelection,
  };
};
