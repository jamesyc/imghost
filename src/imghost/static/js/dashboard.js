const flash = document.getElementById("flash");
const bootstrapNode = document.getElementById("dashboard-bootstrap");
const usageSummary = document.getElementById("dashboard-usage-summary");
const usageCopy = document.getElementById("dashboard-usage-copy");
const quotaCopy = document.getElementById("dashboard-quota-copy");
const usageBar = document.getElementById("dashboard-usage-bar");
const recentAlbumsRoot = document.getElementById("dashboard-recent-albums");
const emptyState = document.getElementById("dashboard-recent-albums-empty");
const bootstrap = bootstrapNode ? JSON.parse(bootstrapNode.textContent || "{}") : {};
const uploadForm = document.getElementById("dashboard-upload-form");
const uploadDropzone = uploadForm?.querySelector(".upload-dropzone");
const uploadInput = uploadForm?.querySelector('input[type="file"][name="file"]');
const uploadPasteInput = document.getElementById("dashboard-upload-paste-input");
const uploadPicker = uploadForm?.querySelector(".upload-picker");
const uploadStatus = window.createUploadStatusController?.(uploadForm);

const state = {
  user: bootstrap.session_user || null,
  uploadInFlight: false,
};

const showMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
  }
};

const formatBytes = window.albumCardFormatBytes;

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

const updateUsageSummary = () => {
  if (!state.user || !usageSummary || !usageCopy || !quotaCopy || !usageBar) {
    return;
  }
  const albumCount = Number(state.user.album_count || 0);
  const mediaCount = Number(state.user.media_count || 0);
  const usedBytes = Number(state.user.storage_used_bytes || 0);
  const quotaBytes = Number(state.user.quota_bytes || 0);
  const percent = quotaBytes > 0 ? Math.min(100, Math.max(0, (usedBytes / quotaBytes) * 100)) : 0;
  const summaryHeading = usageSummary.querySelector("h2");
  if (summaryHeading) {
    summaryHeading.textContent = `${albumCount} album${albumCount === 1 ? "" : "s"}`;
  }
  usageCopy.textContent = `${mediaCount} file${mediaCount === 1 ? "" : "s"} stored.`;
  quotaCopy.textContent = `Using ${formatBytes(usedBytes)} of ${formatBytes(quotaBytes)}.`;
  usageBar.style.width = `${percent}%`;
};

const refreshUser = async () => {
  state.user = await requestJson("/api/v1/user/me");
  updateUsageSummary();
};

const refreshRecentAlbums = async () => {
  const albums = await requestJson("/api/v1/user/me/albums");
  const recentAlbums = albums.slice(0, 5);
  recentAlbumsRoot.innerHTML = recentAlbums.length ? recentAlbums.map((album) => window.renderAlbumCard(album)).join("") : "";
  emptyState?.classList.toggle("hidden", recentAlbums.length > 0);
};

const setUploadingState = (isUploading) => {
  state.uploadInFlight = isUploading;
  uploadDropzone?.classList.toggle("is-uploading", isUploading);
  uploadPasteInput?.classList.toggle("is-uploading", isUploading);
  uploadPicker?.classList.toggle("is-uploading", isUploading);
  uploadForm?.querySelector(".upload-file-summary")?.classList.toggle("is-uploading", isUploading);
  if (uploadInput) {
    uploadInput.disabled = isUploading;
  }
  if (uploadPasteInput) {
    uploadPasteInput.disabled = isUploading;
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
  if (uploadPasteInput) {
    uploadPasteInput.value = "";
  }
  updateFileSummary();
};

const submitUpload = async (fileList, statusMessage) => {
  if (!uploadForm || !fileList?.length) {
    return;
  }
  if (state.uploadInFlight) {
    uploadStatus?.setError("Upload already in progress.");
    return;
  }
  let shouldResetInputs = true;
  setUploadingState(true);
  updateFileSummary(statusMessage || `Uploading ${fileList.length} file${fileList.length === 1 ? "" : "s"}...`);
  try {
    const formData = new FormData();
    const titleInput = uploadForm.querySelector('input[name="title"]');
    const albumInput = uploadForm.querySelector('input[name="album_id"]');
    if (titleInput?.value) {
      formData.set("title", titleInput.value);
    }
    if (albumInput?.value) {
      formData.set("album_id", albumInput.value);
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
    shouldResetInputs = false;
    uploadForm.reset();
    resetTransientInputs();
    const destination = window.resolveUploadDestination?.({
      albumId: data.album_id,
      albumUrl: data.album_url,
      isAuthenticated: true,
    }) || "";
    if (destination) {
      uploadStatus?.persistSuccess("Upload succeeded. Redirecting...");
      window.setTimeout(() => {
        window.location.assign(destination);
      }, 150);
      return;
    }
    await refreshUser();
    await refreshRecentAlbums();
    uploadStatus?.setSuccess(data.album_id ? `Upload succeeded. Album ${data.album_id} is ready.` : "Upload succeeded.");
  } catch (error) {
    uploadStatus?.setError(error.message || statusMessage || "Upload failed");
  } finally {
    if (shouldResetInputs) {
      resetTransientInputs();
    }
    setUploadingState(false);
  }
};

const inferFileName = (url, contentType) => {
  try {
    const pathname = new URL(url).pathname;
    const lastSegment = pathname.split("/").filter(Boolean).pop();
    if (lastSegment?.includes(".")) {
      return lastSegment;
    }
  } catch {
    // use generated name below
  }
  const subtype = contentType?.split("/")[1]?.split(";")[0] || "bin";
  return `pasted-image.${subtype}`;
};

const uploadFromUrl = async (urlText) => {
  const url = urlText.trim();
  if (!url) {
    return;
  }
  if (state.uploadInFlight) {
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

uploadPasteInput?.addEventListener("paste", (event) => {
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
    uploadPasteInput.value = "";
    updateFileSummary(pastedFiles.length === 1 ? "Pasted image ready" : `${pastedFiles.length} pasted images ready`);
    submitUpload(pastedFiles, "Uploading pasted image...");
    return;
  }
  const pastedText = clipboard.getData("text").trim();
  if (pastedText) {
    event.preventDefault();
    uploadPasteInput.value = pastedText;
    uploadFromUrl(pastedText);
  }
});

updateUsageSummary();
Promise.all([refreshUser(), refreshRecentAlbums()]).catch((error) => {
  showMessage(error.message);
});
window.attachAlbumCardNavigation?.(recentAlbumsRoot);
