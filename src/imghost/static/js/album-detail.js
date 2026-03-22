const flash = document.getElementById("flash");
const bootstrapNode = document.getElementById("album-detail-bootstrap");
const titleNode = document.getElementById("album-detail-title");
const summaryNode = document.getElementById("album-detail-summary");
const actionsNode = document.getElementById("album-detail-actions");
const metadataForm = document.getElementById("album-detail-metadata-form");
const metadataActionsNode = document.getElementById("album-detail-metadata-actions");
const titleInput = document.getElementById("album-detail-title-input");
const itemsRoot = document.getElementById("album-detail-items");
const addImagesButton = document.getElementById("album-detail-add-images-button");
const uploadModal = document.getElementById("album-upload-modal");
const uploadForm = document.getElementById("album-upload-form");
const uploadPasteInput = document.getElementById("album-upload-paste-input");
const bootstrap = bootstrapNode ? JSON.parse(bootstrapNode.textContent || "{}") : {};

const state = {
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

let albumUploadController = null;
let titleToastTimer = null;

const initializeAnonymousManageAccess = () => {
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

const showMessage = (message) => {
  if (flash) {
    flash.textContent = message || "";
  }
};

const showToast = (message) => {
  let toast = document.getElementById("album-detail-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "album-detail-toast";
    toast.className = "album-detail-toast hidden";
    toast.setAttribute("aria-live", "polite");
    document.body.append(toast);
  }
  toast.textContent = message || "";
  toast.classList.remove("hidden");
  window.clearTimeout(titleToastTimer);
  titleToastTimer = window.setTimeout(() => {
    toast.classList.add("hidden");
  }, 2200);
};

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

const formatBytes = (value) => {
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

const withAlbumAccess = (url) => {
  if (!state.deleteToken) {
    return url;
  }
  const resolved = new URL(url, window.location.origin);
  if (!resolved.searchParams.has("delete_token")) {
    resolved.searchParams.set("delete_token", state.deleteToken);
  }
  return `${resolved.pathname}${resolved.search}`;
};

const requestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

const publicAlbumUrl = (albumId) => `${window.location.origin}/a/${albumId}`;
const manageAlbumUrl = (albumId) =>
  state.deleteToken && window.imghostAnonAlbums?.manageUrl
    ? `${window.location.origin}${window.imghostAnonAlbums.manageUrl(albumId, state.deleteToken)}`
    : "";

const splitLinkControl = (label, url, ariaLabel) => `
  <div class="split-link-control">
    <a class="split-link-action" href="${escapeHtml(url)}">${escapeHtml(label)}</a>
    <input
      class="split-link-value"
      type="text"
      value="${escapeHtml(url)}"
      readonly
      aria-label="${escapeHtml(ariaLabel)}"
      onclick="this.select()"
    >
  </div>
`;

const dragHandleMarkup = () => `
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

const orderedMediaIds = () =>
  Array.from(itemsRoot?.querySelectorAll(".album-detail-item[data-media-id]") || []).map((node) => node.dataset.mediaId);

const setUploadModalOpen = (open) => {
  if (!uploadModal) {
    return;
  }
  uploadModal.classList.toggle("hidden", !open);
  uploadModal.setAttribute("aria-hidden", open ? "false" : "true");
  document.body.classList.toggle("upload-modal-open", open);
  if (!open) {
    uploadForm?.reset();
    albumUploadController?.resetTransientInputs?.();
  }
};

const renderActions = (album) => {
  if (actionsNode) {
    const links = [splitLinkControl("Public Page", publicAlbumUrl(album.id), "Public page URL")];
    if (state.accessMode === "token" && state.deleteToken) {
      links.push(splitLinkControl("Manage Link", manageAlbumUrl(album.id), "Private manage link URL"));
    }
    actionsNode.innerHTML = links.join("");
  }
  if (metadataActionsNode) {
    metadataActionsNode.innerHTML = `
      <button type="submit">Update Album Title</button>
      <a class="button-link secondary-link" href="/api/v1/album/${album.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
      <button id="album-detail-delete" type="button" class="danger">Delete Album</button>
    `;
  }
};

const ensureLightbox = () => {
  let lightbox = document.getElementById("album-detail-lightbox");
  if (lightbox) {
    return lightbox;
  }
  lightbox = document.createElement("div");
  lightbox.id = "album-detail-lightbox";
  lightbox.className = "album-detail-lightbox hidden";
  lightbox.innerHTML = `
    <button type="button" class="album-detail-lightbox-backdrop" aria-label="Close preview"></button>
    <div class="album-detail-lightbox-dialog" role="dialog" aria-modal="true" aria-label="Media preview">
      <button type="button" class="album-detail-lightbox-close" aria-label="Close preview">Close</button>
      <img class="album-detail-lightbox-image" alt="">
    </div>
  `;
  document.body.append(lightbox);
  return lightbox;
};

const closeLightbox = () => {
  const lightbox = document.getElementById("album-detail-lightbox");
  if (!lightbox) {
    return;
  }
  lightbox.classList.add("hidden");
  document.body.classList.remove("lightbox-open");
  state.lightboxMediaUrl = null;
};

const openLightbox = (mediaUrl, filename) => {
  const lightbox = ensureLightbox();
  const image = lightbox.querySelector(".album-detail-lightbox-image");
  if (!image) {
    return;
  }
  state.lightboxMediaUrl = mediaUrl;
  image.src = mediaUrl;
  image.alt = filename || "Album media preview";
  lightbox.classList.remove("hidden");
  document.body.classList.add("lightbox-open");
};

const albumDisplayTitle = (album) => album?.title || "Untitled album";

const updateTitleEditor = (album) => {
  const displayTitle = albumDisplayTitle(album);
  if (titleNode) {
    titleNode.textContent = displayTitle;
    titleNode.setAttribute("aria-label", `Edit album title ${displayTitle}`);
  }
  if (titleInput && !state.titleEditing) {
    titleInput.value = album?.title || "";
  }
};

const setTitleEditing = (editing) => {
  state.titleEditing = editing;
  titleNode?.classList.toggle("hidden", editing);
  titleInput?.classList.toggle("hidden", !editing);
  if (editing) {
    titleInput?.focus();
    titleInput?.select();
  }
};

const renderAlbum = (album) => {
  state.album = album;
  updateTitleEditor(album);
  summaryNode.textContent = `Album ${album.id} · ${album.item_count} item(s) · ${formatBytes(album.total_size)} · updated ${new Date(album.updated_at).toLocaleString()}`;
  renderActions(album);
  metadataForm?.classList.remove("hidden");
  if (!state.titleEditing) {
    setTitleEditing(false);
  }
  itemsRoot.innerHTML = album.items.map((item) => `
    <section
      class="album-item album-detail-item${album.cover_media_id === item.id ? " is-cover" : ""}"
      data-media-id="${item.id}"
    >
      <div class="album-item-meta">
        <div class="album-item-title-row">
          ${dragHandleMarkup()}
          <h3>${escapeHtml(item.filename || item.id)}</h3>
          <p class="hint">${formatBytes(item.file_size)}</p>
        </div>
        <button
          type="button"
          class="album-detail-thumb"
          data-lightbox-url="${escapeHtml(item.media_url)}"
          data-lightbox-filename="${escapeHtml(item.filename || item.id)}"
          aria-label="Preview ${escapeHtml(item.filename || item.id)}"
        >
          <img src="${escapeHtml(item.media_url)}" alt="${escapeHtml(item.filename || item.id)}">
        </button>
        ${splitLinkControl("Media Link", item.media_url, "Media URL")}
        <div class="row row-actions album-detail-item-actions">
          <button type="button" class="secondary album-detail-move-button" data-direction="-1" data-media-id="${item.id}">Move Up</button>
          <button type="button" class="secondary album-detail-move-button" data-direction="1" data-media-id="${item.id}">Move Down</button>
          <button type="button" class="secondary album-detail-cover-button" data-media-id="${item.id}">Set As Album Cover</button>
          <button type="button" class="danger album-detail-delete-media" data-media-id="${item.id}">Delete Media</button>
        </div>
      </div>
    </section>
  `).join("");
};

const loadAlbum = async () => {
  if (!state.albumId) {
    showMessage("Missing album ID.");
    return;
  }
  const album = await requestJson(withAlbumAccess(`/api/v1/album/${state.albumId}`));
  renderAlbum(album);
};

initializeAnonymousManageAccess();

const persistOrder = async (mediaIds, successMessage = "Album order updated.") => {
  if (!state.albumId || !mediaIds.length || state.reorderInFlight) {
    return;
  }
  state.reorderInFlight = true;
  showMessage("Saving album order...");
  try {
    const album = await requestJson(withAlbumAccess(`/api/v1/album/${state.albumId}/order`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mediaIds.map((mediaId, index) => ({
        media_id: mediaId,
        position: (index + 1) * 1000,
      }))),
    });
    renderAlbum(album);
    showMessage(successMessage);
  } catch (error) {
    showMessage(error.message);
    await loadAlbum();
  } finally {
    state.reorderInFlight = false;
  }
};

const moveItemByOffset = async (mediaId, offset) => {
  const mediaIds = orderedMediaIds();
  const index = mediaIds.indexOf(mediaId);
  if (index === -1) {
    return;
  }
  const nextIndex = index + offset;
  if (nextIndex < 0 || nextIndex >= mediaIds.length) {
    return;
  }
  const reordered = [...mediaIds];
  const [moved] = reordered.splice(index, 1);
  reordered.splice(nextIndex, 0, moved);
  await persistOrder(reordered);
};

const persistTitle = async ({ keepEditingOnError = true } = {}) => {
  if (!state.albumId || !state.album || !titleInput || state.titleSaving) {
    return;
  }
  const nextTitle = titleInput.value.trim();
  const currentTitle = state.album.title || "";
  if (nextTitle === currentTitle) {
    setTitleEditing(false);
    return;
  }
  state.titleSaving = true;
  try {
    const album = await requestJson(withAlbumAccess(`/api/v1/album/${state.albumId}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: nextTitle || null }),
    });
    renderAlbum(album);
    setTitleEditing(false);
    showToast("Album title updated.");
  } catch (error) {
    showMessage(error.message);
    if (!keepEditingOnError) {
      titleInput.value = state.album.title || "";
      setTitleEditing(false);
    } else {
      setTitleEditing(true);
    }
  } finally {
    state.titleSaving = false;
  }
};

const persistCover = async (mediaId) => {
  if (!state.albumId) {
    return;
  }
  const album = await requestJson(withAlbumAccess(`/api/v1/album/${state.albumId}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cover_media_id: mediaId }),
  });
  renderAlbum(album);
  showMessage("Cover updated.");
};

metadataForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.albumId) {
    return;
  }
  if (!state.titleEditing) {
    setTitleEditing(true);
    return;
  }
  await persistTitle();
});

titleNode?.addEventListener("click", () => {
  if (!state.titleSaving) {
    setTitleEditing(true);
  }
});

titleInput?.addEventListener("blur", async () => {
  if (state.titleEditing) {
    await persistTitle();
  }
});

titleInput?.addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    await persistTitle();
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    titleInput.value = state.album?.title || "";
    setTitleEditing(false);
  }
});

metadataActionsNode?.addEventListener("click", async (event) => {
  const albumDeleteButton = event.target.closest("#album-detail-delete");
  if (!albumDeleteButton || !state.albumId || !window.confirm("Delete this album?")) {
    return;
  }
  try {
    await requestJson(withAlbumAccess(`/api/v1/album/${state.albumId}`), { method: "DELETE" });
    window.location.assign(state.postDeleteUrl);
  } catch (error) {
    showMessage(error.message);
  }
});

itemsRoot?.addEventListener("click", async (event) => {
  const lightboxButton = event.target.closest(".album-detail-thumb");
  if (lightboxButton) {
    openLightbox(lightboxButton.dataset.lightboxUrl, lightboxButton.dataset.lightboxFilename);
    return;
  }

  const moveButton = event.target.closest(".album-detail-move-button");
  if (moveButton && !state.reorderInFlight) {
    await moveItemByOffset(moveButton.dataset.mediaId, Number(moveButton.dataset.direction || 0));
    return;
  }

  const coverButton = event.target.closest(".album-detail-cover-button");
  if (coverButton) {
    try {
      await persistCover(coverButton.dataset.mediaId);
    } catch (error) {
      showMessage(error.message);
    }
    return;
  }

  const deleteMediaButton = event.target.closest(".album-detail-delete-media");
  if (deleteMediaButton) {
    if (!window.confirm("Delete this media item?")) {
      return;
    }
    try {
      const result = await requestJson(withAlbumAccess(`/api/v1/media/${deleteMediaButton.dataset.mediaId}`), { method: "DELETE" });
      if (result.album_deleted) {
        window.location.assign(state.postDeleteUrl);
        return;
      }
      await loadAlbum();
      showMessage("Media deleted.");
    } catch (error) {
      showMessage(error.message);
    }
  }
});

itemsRoot?.addEventListener("dragstart", (event) => {
  const handle = event.target.closest(".album-detail-drag-handle");
  if (!handle || state.reorderInFlight) {
    event.preventDefault();
    return;
  }
  const card = handle.closest(".album-detail-item");
  if (!card?.dataset.mediaId || !event.dataTransfer) {
    event.preventDefault();
    return;
  }
  state.draggedMediaId = card.dataset.mediaId;
  card.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", state.draggedMediaId);
});

itemsRoot?.addEventListener("dragover", (event) => {
  if (!state.draggedMediaId || state.reorderInFlight) {
    return;
  }
  const targetCard = event.target.closest(".album-detail-item");
  if (!targetCard || targetCard.dataset.mediaId === state.draggedMediaId) {
    return;
  }
  event.preventDefault();
  const draggedCard = itemsRoot.querySelector(`.album-detail-item[data-media-id="${state.draggedMediaId}"]`);
  if (!draggedCard) {
    return;
  }
  const { top, height } = targetCard.getBoundingClientRect();
  const insertAfter = event.clientY > top + (height / 2);
  targetCard.classList.toggle("drop-before", !insertAfter);
  targetCard.classList.toggle("drop-after", insertAfter);
  if (insertAfter) {
    targetCard.after(draggedCard);
  } else {
    targetCard.before(draggedCard);
  }
});

itemsRoot?.addEventListener("dragleave", (event) => {
  const targetCard = event.target.closest(".album-detail-item");
  if (targetCard) {
    targetCard.classList.remove("drop-before", "drop-after");
  }
});

itemsRoot?.addEventListener("dragend", async () => {
  const draggedMediaId = state.draggedMediaId;
  state.draggedMediaId = null;
  itemsRoot.querySelectorAll(".album-detail-item").forEach((node) => {
    node.classList.remove("is-dragging", "drop-before", "drop-after");
  });
  if (!draggedMediaId || state.reorderInFlight) {
    return;
  }
  const mediaIds = orderedMediaIds();
  if (mediaIds.join(",") === (state.album?.items || []).map((item) => item.id).join(",")) {
    return;
  }
  await persistOrder(mediaIds);
});

document.addEventListener("click", (event) => {
  if (event.target.closest(".album-detail-lightbox-backdrop, .album-detail-lightbox-close")) {
    closeLightbox();
    return;
  }
  if (event.target.closest("#album-upload-modal-backdrop, #album-upload-modal-close")) {
    setUploadModalOpen(false);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.lightboxMediaUrl) {
    closeLightbox();
    return;
  }
  if (event.key === "Escape" && !uploadModal?.classList.contains("hidden")) {
    setUploadModalOpen(false);
  }
});

addImagesButton?.addEventListener("click", () => {
  setUploadModalOpen(true);
});

albumUploadController = window.attachUploadBox?.({
  uploadForm,
  pasteInput: uploadPasteInput,
  isAuthenticated: state.accessMode === "owner",
  fixedAlbumId: state.albumId,
  fixedDeleteToken: state.deleteToken,
  onError: showMessage,
  onSuccess: async ({ response }) => {
    await loadAlbum();
    const addedCount = Array.isArray(response.items) ? response.items.length : 0;
    window.setTimeout(() => {
      setUploadModalOpen(false);
    }, 450);
    return { successMessage: addedCount === 1 ? "Image added." : "Images added." };
  },
});

if (state.deleteToken) {
  window.imghostAnonAlbums?.remember({ albumId: state.albumId, deleteToken: state.deleteToken });
}

loadAlbum().catch((error) => {
  showMessage(error.message);
  titleNode.textContent = "Album unavailable";
  summaryNode.textContent = "This album could not be loaded.";
});
