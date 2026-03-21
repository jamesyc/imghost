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

window.attachUploadBox = ({
  uploadForm,
  pasteInput,
  isAuthenticated = false,
  fixedAlbumId = "",
  fixedDeleteToken = "",
  onSuccess,
  onError,
} = {}) => {
  if (!uploadForm) {
    return null;
  }

  const uploadDropzone = uploadForm.querySelector(".upload-dropzone");
  const uploadInput = uploadForm.querySelector('input[type="file"][name="file"]');
  const uploadPicker = uploadForm.querySelector(".upload-picker");
  const uploadStatus = window.createUploadStatusController?.(uploadForm);
  let uploadInFlight = false;

  const setUploadingState = (isUploading) => {
    uploadInFlight = isUploading;
    uploadDropzone?.classList.toggle("is-uploading", isUploading);
    pasteInput?.classList.toggle("is-uploading", isUploading);
    uploadPicker?.classList.toggle("is-uploading", isUploading);
    uploadForm.querySelector(".upload-file-summary")?.classList.toggle("is-uploading", isUploading);
    if (uploadInput) {
      uploadInput.disabled = isUploading;
    }
    if (pasteInput) {
      pasteInput.disabled = isUploading;
    }
  };

  const updateFileSummary = (message) => {
    if (!uploadStatus) {
      return;
    }
    if (message) {
      uploadStatus.setInfo(message);
      return;
    }
    uploadStatus.syncSelection(uploadInput);
  };

  const resetTransientInputs = () => {
    if (uploadInput) {
      uploadInput.value = "";
    }
    if (pasteInput) {
      pasteInput.value = "";
    }
    updateFileSummary();
  };

  const inferFileName = (url, contentType) => {
    try {
      const pathname = new URL(url).pathname;
      const lastSegment = pathname.split("/").filter(Boolean).pop();
      if (lastSegment?.includes(".")) {
        return lastSegment;
      }
    } catch {
      // ignore URL parsing failures and use the generated name below
    }
    const subtype = contentType?.split("/")[1]?.split(";")[0] || "bin";
    return `pasted-image.${subtype}`;
  };

  const submitUpload = async (fileList, statusMessage) => {
    if (!fileList?.length) {
      return;
    }
    if (uploadInFlight) {
      uploadStatus?.setError("Upload already in progress.");
      return;
    }

    let shouldResetInputs = true;
    setUploadingState(true);
    updateFileSummary(statusMessage || `Uploading ${fileList.length} file${fileList.length === 1 ? "" : "s"}...`);

    try {
      const formData = new FormData();
      const titleInput = uploadForm.querySelector('input[name="title"]');
      if (titleInput?.value) {
        formData.set("title", titleInput.value);
      }
      if (fixedAlbumId) {
        formData.set("album_id", fixedAlbumId);
      }
      if (fixedDeleteToken) {
        formData.set("delete_token", fixedDeleteToken);
      }
      Array.from(fileList).forEach((file) => {
        formData.append("file", file, file.name);
      });

      const response = await fetch("/api/v1/upload", {
        method: "POST",
        body: formData,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || "Upload failed.");
      }

      if (!isAuthenticated && data.album_id) {
        const deleteToken = window.imghostAnonAlbums?.deleteTokenFromDeleteUrl?.(data.delete_url || "");
        if (deleteToken) {
          window.imghostAnonAlbums?.remember?.({ albumId: data.album_id, deleteToken });
        }
      }

      const successResult = await onSuccess?.({
        response: data,
        uploadStatus,
        uploadForm,
        resetTransientInputs,
        updateFileSummary,
      });

      const redirectTo = successResult?.redirectTo
        ?? window.resolveUploadDestination?.({
          albumId: data.album_id,
          albumUrl: data.album_url,
          isAuthenticated,
        })
        ?? "";

      if (redirectTo) {
        shouldResetInputs = false;
        uploadStatus?.persistSuccess(successResult?.redirectMessage || "Upload succeeded. Redirecting...");
        window.setTimeout(() => {
          window.location.assign(redirectTo);
        }, 150);
        return;
      }

      uploadForm.reset();
      resetTransientInputs();
      if (successResult?.successMessage) {
        uploadStatus?.setSuccess(successResult.successMessage);
      }
    } catch (error) {
      const message = error.message || statusMessage || "Upload failed";
      uploadStatus?.setError(message);
      onError?.(message);
    } finally {
      if (shouldResetInputs) {
        resetTransientInputs();
      }
      setUploadingState(false);
    }
  };

  const uploadFromUrl = async (urlText) => {
    const url = urlText.trim();
    if (!url) {
      return;
    }
    if (uploadInFlight) {
      uploadStatus?.setError("Upload already in progress.");
      return;
    }
    try {
      new URL(url);
    } catch {
      uploadStatus?.setError("Paste a valid image URL.");
      return;
    }
    updateFileSummary("Fetching image URL...");
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error("Could not fetch that image URL.");
      }
      const blob = await response.blob();
      if (!blob.type.startsWith("image/")) {
        throw new Error("That URL did not return an image.");
      }
      const file = new File([blob], inferFileName(url, blob.type), { type: blob.type });
      await submitUpload([file], "Uploading pasted image URL...");
    } catch (error) {
      updateFileSummary();
      uploadStatus?.setError(error.message || "Could not fetch that image URL.");
      onError?.(error.message || "Could not fetch that image URL.");
    }
  };

  ["dragenter", "dragover"].forEach((eventName) => {
    uploadDropzone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      uploadDropzone.classList.add("is-dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    uploadDropzone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      uploadDropzone.classList.remove("is-dragover");
    });
  });

  uploadInput?.addEventListener("change", updateFileSummary);
  updateFileSummary();

  uploadDropzone?.addEventListener("drop", (event) => {
    if (!uploadInput || !event.dataTransfer?.files?.length) {
      return;
    }
    uploadInput.files = event.dataTransfer.files;
    updateFileSummary();
    submitUpload(event.dataTransfer.files);
  });

  uploadInput?.addEventListener("change", () => {
    if (uploadInput.files?.length) {
      submitUpload(uploadInput.files);
    }
  });

  pasteInput?.addEventListener("paste", (event) => {
    const clipboard = event.clipboardData;
    if (!clipboard) {
      return;
    }
    const pastedFiles = Array.from(clipboard.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (pastedFiles.length) {
      event.preventDefault();
      pasteInput.value = "";
      updateFileSummary(pastedFiles.length === 1 ? "Pasted image ready" : `${pastedFiles.length} pasted images ready`);
      submitUpload(pastedFiles, "Uploading pasted image...");
      return;
    }
    const pastedText = clipboard.getData("text").trim();
    if (pastedText) {
      event.preventDefault();
      pasteInput.value = pastedText;
      uploadFromUrl(pastedText);
    }
  });

  return {
    submitUpload,
    updateFileSummary,
    resetTransientInputs,
    setUploadingState,
    status: uploadStatus,
    isUploading: () => uploadInFlight,
  };
};
