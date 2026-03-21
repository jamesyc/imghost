const flash = document.getElementById("flash");
const uploadForm = document.getElementById("upload-form");
const uploadDropzone = uploadForm?.querySelector(".upload-dropzone");
const uploadInput = uploadForm?.querySelector('input[type="file"][name="file"]');
const uploadPasteInput = document.getElementById("upload-paste-input");
const uploadPicker = uploadForm?.querySelector(".upload-picker");
const uploadStatus = window.createUploadStatusController?.(uploadForm);
const authenticatedUpload = Boolean(uploadForm?.querySelector('input[name="album_id"]'));
let uploadInFlight = false;

const showHomeMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
  }
};

const setUploadingState = (isUploading) => {
  uploadInFlight = isUploading;
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
    const destination = window.resolveUploadDestination?.({
      albumId: data.album_id,
      albumUrl: data.album_url,
      isAuthenticated: authenticatedUpload,
    }) || "";
    if (!destination) {
      throw new Error("Upload succeeded, but no album URL was returned.");
    }
    shouldResetInputs = false;
    uploadStatus?.persistSuccess("Upload succeeded. Redirecting...");
    window.location.href = destination;
    return;
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
    // ignore URL parsing failures and use a fallback name below
  }
  const subtype = contentType?.split("/")[1]?.split(";")[0] || "bin";
  return `pasted-image.${subtype}`;
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
  }
};

document.getElementById("logout-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await fetch("/api/v1/auth/logout", { method: "POST" });
    window.location.reload();
  } catch {
    showHomeMessage("Logout failed.");
  }
});

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
