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
const uploadPasteInput = document.getElementById("dashboard-upload-paste-input");

const state = {
  user: bootstrap.session_user || null,
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
  const payload = await requestJson("/api/v1/user/me/albums?limit=5&offset=0");
  const recentAlbums = payload.items || [];
  recentAlbumsRoot.innerHTML = recentAlbums.length ? recentAlbums.map((album) => window.renderAlbumCard(album)).join("") : "";
  emptyState?.classList.toggle("hidden", recentAlbums.length > 0);
};

updateUsageSummary();
Promise.all([refreshUser(), refreshRecentAlbums()]).catch((error) => {
  showMessage(error.message);
});
window.attachUploadBox?.({
  uploadForm,
  pasteInput: uploadPasteInput,
  isAuthenticated: true,
  onError: showMessage,
});
window.attachAlbumCardNavigation?.(recentAlbumsRoot);
