const flash = document.getElementById("flash");
const uploadForm = document.getElementById("upload-form");
const uploadPasteInput = document.getElementById("upload-paste-input");
const authenticatedUpload = uploadForm?.dataset.isAuthenticatedUpload === "true";

const showHomeMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
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

window.attachUploadBox?.({
  uploadForm,
  pasteInput: uploadPasteInput,
  isAuthenticated: authenticatedUpload,
  onError: showHomeMessage,
});
