const uploadForm = document.getElementById("upload-form");
const uploadPasteInput = document.getElementById("upload-paste-input");
const authenticatedUpload = uploadForm?.dataset.isAuthenticatedUpload === "true";

document.querySelector("[data-logout-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await fetch("/api/v1/auth/logout", { method: "POST" });
    window.location.reload();
  } catch {
    window.alert("Logout failed.");
  }
});

window.attachUploadBox?.({
  uploadForm,
  pasteInput: uploadPasteInput,
  isAuthenticated: authenticatedUpload,
});
