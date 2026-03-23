(() => {
  const ns = (window.ImghostAlbumDetail = window.ImghostAlbumDetail || {});

  const bootstrapNode = document.getElementById("album-detail-bootstrap");
  const bootstrap = bootstrapNode ? JSON.parse(bootstrapNode.textContent || "{}") : {};

  ns.dom = {
    bootstrapNode,
    titleNode: document.getElementById("album-detail-title"),
    summaryNode: document.getElementById("album-detail-summary"),
    statusNode: document.getElementById("album-detail-status"),
    actionsNode: document.getElementById("album-detail-actions"),
    metadataForm: document.getElementById("album-detail-metadata-form"),
    metadataActionsNode: document.getElementById("album-detail-metadata-actions"),
    titleInput: document.getElementById("album-detail-title-input"),
    itemsRoot: document.getElementById("album-detail-items"),
    addImagesButton: document.getElementById("album-detail-add-images-button"),
    uploadModal: document.getElementById("album-upload-modal"),
    uploadForm: document.getElementById("album-upload-form"),
    uploadPasteInput: document.getElementById("album-upload-paste-input"),
  };
  ns.bootstrap = bootstrap;
  ns.state = {
    albumId: bootstrap.album_id || null,
    accessMode: bootstrap.access_mode || "owner",
    deleteToken: bootstrap.delete_token || "",
    postDeleteUrl: bootstrap.post_delete_url || "/albums",
    album: null,
    reorderInFlight: false,
    draggedMediaId: null,
    lightboxMediaUrl: null,
    titleSaving: false,
    titleEditing: false,
  };
  ns.albumUploadController = null;

  ns.initializeAnonymousManageAccess = () => {
    const { state } = ns;
    if (state.accessMode !== "token" || !state.albumId || !state.deleteToken) {
      return;
    }
    const currentUrl = new URL(window.location.href);
    const urlHasToken = currentUrl.searchParams.has("token");
    window.imghostAnonAlbums?.remember?.({ albumId: state.albumId, deleteToken: state.deleteToken });
    if (!urlHasToken) {
      return;
    }
    currentUrl.searchParams.delete("token");
    const nextPath = `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`;
    window.history.replaceState({}, document.title, nextPath || currentUrl.pathname);
  };

  ns.setPageStatus = (message = "") => {
    if (!ns.dom.statusNode) {
      return;
    }
    ns.dom.statusNode.textContent = message || "";
    ns.dom.statusNode.classList.toggle("hidden", !message);
  };

  ns.escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  ns.formatBytes = (value) => {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const scaled = bytes / (1024 ** exponent);
    const decimals = scaled >= 10 || exponent === 0 ? 0 : 1;
    return `${scaled.toFixed(decimals)} ${units[exponent]}`;
  };

  ns.withAlbumAccess = (url) => {
    if (!ns.state.deleteToken) {
      return url;
    }
    const resolved = new URL(url, window.location.origin);
    if (!resolved.searchParams.has("delete_token")) {
      resolved.searchParams.set("delete_token", ns.state.deleteToken);
    }
    return `${resolved.pathname}${resolved.search}`;
  };

  ns.requestJson = async (url, options = {}) => {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || `Request failed (${response.status}).`);
    }
    return data;
  };

  ns.publicAlbumUrl = (albumId) => `${window.location.origin}/a/${albumId}`;
  ns.manageAlbumUrl = (albumId) =>
    ns.state.deleteToken && window.imghostAnonAlbums?.manageUrl
      ? `${window.location.origin}${window.imghostAnonAlbums.manageUrl(albumId, ns.state.deleteToken)}`
      : "";

  ns.splitLinkControl = (label, url, ariaLabel) => `
    <div class="split-link-control">
      <a class="split-link-action" href="${ns.escapeHtml(url)}">${ns.escapeHtml(label)}</a>
      <input
        class="split-link-value"
        type="text"
        value="${ns.escapeHtml(url)}"
        readonly
        aria-label="${ns.escapeHtml(ariaLabel)}"
      >
    </div>
  `;

  ns.dragHandleMarkup = () => `
    <button
      type="button"
      class="ghost album-detail-drag-handle"
      draggable="true"
      aria-label="Drag to reorder"
      title="Drag to reorder"
    >
      <span aria-hidden="true">::</span>
    </button>
  `;

  ns.orderedMediaIds = () =>
    Array.from(ns.dom.itemsRoot?.querySelectorAll(".album-detail-item[data-media-id]") || []).map((node) => node.dataset.mediaId);

  ns.setUploadModalOpen = (open) => {
    if (!ns.dom.uploadModal) {
      return;
    }
    ns.dom.uploadModal.classList.toggle("hidden", !open);
    ns.dom.uploadModal.setAttribute("aria-hidden", open ? "false" : "true");
    document.body.classList.toggle("upload-modal-open", open);
    if (!open) {
      ns.dom.uploadForm?.reset();
      ns.albumUploadController?.resetTransientInputs?.();
    }
  };
})();
