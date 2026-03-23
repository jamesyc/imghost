(() => {
  const ns = window.ImghostAlbumDetail;
  if (!ns) {
    return;
  }

  ns.initializeAnonymousManageAccess();

  ns.dom.metadataForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!ns.state.albumId) {
      return;
    }
    if (!ns.state.titleEditing) {
      ns.setTitleEditing(true);
      return;
    }
    await ns.persistTitle();
  });

  ns.dom.titleNode?.addEventListener("click", () => {
    if (!ns.state.titleSaving) {
      ns.setTitleEditing(true);
    }
  });

  ns.dom.titleInput?.addEventListener("blur", async () => {
    if (ns.state.titleEditing) {
      await ns.persistTitle();
    }
  });

  ns.dom.titleInput?.addEventListener("keydown", async (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      await ns.persistTitle();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      ns.dom.titleInput.value = ns.state.album?.title || "";
      ns.setTitleEditing(false);
    }
  });

  ns.dom.metadataActionsNode?.addEventListener("click", async (event) => {
    const albumDeleteButton = event.target.closest("#album-detail-delete");
    if (!albumDeleteButton || !ns.state.albumId || !window.confirm("Delete this album?")) {
      return;
    }
    try {
      await ns.requestJson(ns.withAlbumAccess(`/api/v1/album/${ns.state.albumId}`), { method: "DELETE" });
      window.location.assign(ns.state.postDeleteUrl);
    } catch (error) {
      window.alert(error.message);
    }
  });

  ns.dom.itemsRoot?.addEventListener("click", async (event) => {
    const lightboxButton = event.target.closest(".album-detail-thumb");
    if (lightboxButton) {
      ns.openLightbox(lightboxButton.dataset.lightboxUrl, lightboxButton.dataset.lightboxFilename);
      return;
    }

    const moveButton = event.target.closest(".album-detail-move-button");
    if (moveButton && !ns.state.reorderInFlight) {
      await ns.moveItemByOffset(moveButton.dataset.mediaId, Number(moveButton.dataset.direction || 0));
      return;
    }

    const coverButton = event.target.closest(".album-detail-cover-button");
    if (coverButton) {
      try {
        await ns.persistCover(coverButton.dataset.mediaId);
      } catch (error) {
        window.alert(error.message);
      }
      return;
    }

    const deleteMediaButton = event.target.closest(".album-detail-delete-media");
    if (!deleteMediaButton) {
      return;
    }
    if (!window.confirm("Delete this media item?")) {
      return;
    }
    try {
      const result = await ns.requestJson(ns.withAlbumAccess(`/api/v1/media/${deleteMediaButton.dataset.mediaId}`), {
        method: "DELETE",
      });
      if (result.album_deleted) {
        window.location.assign(ns.state.postDeleteUrl);
        return;
      }
      await ns.loadAlbum();
    } catch (error) {
      window.alert(error.message);
    }
  });

  ns.dom.itemsRoot?.addEventListener("dragstart", (event) => {
    const handle = event.target.closest(".album-detail-drag-handle");
    if (!handle || ns.state.reorderInFlight) {
      event.preventDefault();
      return;
    }
    const card = handle.closest(".album-detail-item");
    if (!card?.dataset.mediaId || !event.dataTransfer) {
      event.preventDefault();
      return;
    }
    ns.state.draggedMediaId = card.dataset.mediaId;
    card.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", ns.state.draggedMediaId);
  });

  ns.dom.itemsRoot?.addEventListener("dragover", (event) => {
    if (!ns.state.draggedMediaId || ns.state.reorderInFlight) {
      return;
    }
    const targetCard = event.target.closest(".album-detail-item");
    if (!targetCard || targetCard.dataset.mediaId === ns.state.draggedMediaId) {
      return;
    }
    event.preventDefault();
    const draggedCard = ns.dom.itemsRoot.querySelector(`.album-detail-item[data-media-id="${ns.state.draggedMediaId}"]`);
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

  ns.dom.itemsRoot?.addEventListener("dragleave", (event) => {
    const targetCard = event.target.closest(".album-detail-item");
    if (targetCard) {
      targetCard.classList.remove("drop-before", "drop-after");
    }
  });

  ns.dom.itemsRoot?.addEventListener("dragend", async () => {
    const draggedMediaId = ns.state.draggedMediaId;
    ns.state.draggedMediaId = null;
    ns.dom.itemsRoot.querySelectorAll(".album-detail-item").forEach((node) => {
      node.classList.remove("is-dragging", "drop-before", "drop-after");
    });
    if (!draggedMediaId || ns.state.reorderInFlight) {
      return;
    }
    const mediaIds = ns.orderedMediaIds();
    if (mediaIds.join(",") === (ns.state.album?.items || []).map((item) => item.id).join(",")) {
      return;
    }
    await ns.persistOrder(mediaIds);
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest(".album-detail-lightbox-backdrop, .album-detail-lightbox-close")) {
      ns.closeLightbox();
      return;
    }
    if (event.target.closest("#album-upload-modal-backdrop, #album-upload-modal-close")) {
      ns.setUploadModalOpen(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && ns.state.lightboxMediaUrl) {
      ns.closeLightbox();
      return;
    }
    if (event.key === "Escape" && !ns.dom.uploadModal?.classList.contains("hidden")) {
      ns.setUploadModalOpen(false);
    }
  });

  ns.dom.addImagesButton?.addEventListener("click", () => {
    ns.setUploadModalOpen(true);
  });

  ns.albumUploadController = window.attachUploadBox?.({
    uploadForm: ns.dom.uploadForm,
    pasteInput: ns.dom.uploadPasteInput,
    isAuthenticated: ns.state.accessMode === "owner",
    fixedAlbumId: ns.state.albumId,
    fixedDeleteToken: ns.state.deleteToken,
    onSuccess: async ({ response }) => {
      await ns.loadAlbum();
      const addedCount = Array.isArray(response.items) ? response.items.length : 0;
      window.setTimeout(() => {
        ns.setUploadModalOpen(false);
      }, 450);
      return { successMessage: addedCount === 1 ? "Image added." : "Images added." };
    },
  });

  if (ns.state.deleteToken) {
    window.imghostAnonAlbums?.remember({ albumId: ns.state.albumId, deleteToken: ns.state.deleteToken });
  }

  ns.loadAlbum().catch((error) => {
    ns.setPageStatus(error.message);
    if (ns.dom.titleNode) {
      ns.dom.titleNode.textContent = "Album unavailable";
    }
    if (ns.dom.summaryNode) {
      ns.dom.summaryNode.textContent = "This album could not be loaded.";
    }
  });
})();
