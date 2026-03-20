const flash = document.getElementById("flash");
const uploadResult = document.getElementById("upload-result");

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

document.getElementById("upload-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const form = event.currentTarget;
    const response = await fetch("/api/v1/upload", {
      method: "POST",
      body: new FormData(form),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || "Upload failed.");
    }
    if (uploadResult) {
      uploadResult.classList.remove("hidden");
      uploadResult.textContent = JSON.stringify(data, null, 2);
    }
    showHomeMessage("Upload succeeded.");
  } catch (error) {
    showHomeMessage(error.message);
  }
});
