(() => {
  const ns = window.ImghostAlbumDetail;
  if (!ns) {
    return;
  }

  ns.renderActions = (album) => {
    if (ns.dom.actionsNode) {
      const links = [ns.splitLinkControl("Public Page", ns.publicAlbumUrl(album.id), "Public page URL")];
      if (ns.state.accessMode === "token" && ns.state.deleteToken) {
        links.push(ns.splitLinkControl("Manage Link", ns.manageAlbumUrl(album.id), "Private manage link URL"));
      }
      ns.dom.actionsNode.innerHTML = links.join("");
    }
    if (ns.dom.metadataActionsNode) {
      ns.dom.metadataActionsNode.innerHTML = `
        <button type="submit">Update Album Title</button>
        <a class="button-link secondary-link" href="/api/v1/album/${album.id}/zip" target="_blank" rel="noreferrer">Download ZIP</a>
        <button id="album-detail-delete" type="button" class="danger">Delete Album</button>
      `;
    }
  };

  ns.ensureLightbox = () => {
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

  ns.closeLightbox = () => {
    const lightbox = document.getElementById("album-detail-lightbox");
    if (!lightbox) {
      return;
    }
    lightbox.classList.add("hidden");
    document.body.classList.remove("lightbox-open");
    ns.state.lightboxMediaUrl = null;
  };

  ns.openLightbox = (mediaUrl, filename) => {
    const lightbox = ns.ensureLightbox();
    const image = lightbox.querySelector(".album-detail-lightbox-image");
    if (!image) {
      return;
    }
    ns.state.lightboxMediaUrl = mediaUrl;
    image.src = mediaUrl;
    image.alt = filename || "Album media preview";
    lightbox.classList.remove("hidden");
    document.body.classList.add("lightbox-open");
  };

  ns.albumDisplayTitle = (album) => album?.title || "Untitled album";

  ns.updateTitleEditor = (album) => {
    const displayTitle = ns.albumDisplayTitle(album);
    if (ns.dom.titleNode) {
      ns.dom.titleNode.textContent = displayTitle;
      ns.dom.titleNode.setAttribute("aria-label", `Edit album title ${displayTitle}`);
    }
    if (ns.dom.titleInput && !ns.state.titleEditing) {
      ns.dom.titleInput.value = album?.title || "";
    }
  };

  ns.setTitleEditing = (editing) => {
    ns.state.titleEditing = editing;
    ns.dom.titleNode?.classList.toggle("hidden", editing);
    ns.dom.titleInput?.classList.toggle("hidden", !editing);
    if (editing) {
      ns.dom.titleInput?.focus();
      ns.dom.titleInput?.select();
    }
  };

  ns.renderAlbum = (album) => {
    ns.state.album = album;
    ns.updateTitleEditor(album);
    if (ns.dom.summaryNode) {
      ns.dom.summaryNode.textContent =
        `Album ${album.id} · ${album.item_count} item(s) · ${ns.formatBytes(album.total_size)} · updated ${new Date(album.updated_at).toLocaleString()}`;
    }
    ns.renderActions(album);
    ns.dom.metadataForm?.classList.remove("hidden");
    if (!ns.state.titleEditing) {
      ns.setTitleEditing(false);
    }
    if (!ns.dom.itemsRoot) {
      return;
    }
    ns.dom.itemsRoot.innerHTML = album.items.map((item) => `
      <section
        class="album-item album-detail-item${album.cover_media_id === item.id ? " is-cover" : ""}"
        data-media-id="${item.id}"
      >
        <div class="album-item-meta">
          <div class="album-item-title-row">
            ${ns.dragHandleMarkup()}
            <h3>${ns.escapeHtml(item.filename || item.id)}</h3>
            <p class="hint">${ns.formatBytes(item.file_size)}</p>
          </div>
          <button
            type="button"
            class="album-detail-thumb"
            data-lightbox-url="${ns.escapeHtml(item.media_url)}"
            data-lightbox-filename="${ns.escapeHtml(item.filename || item.id)}"
            aria-label="Preview ${ns.escapeHtml(item.filename || item.id)}"
          >
            <img src="${ns.escapeHtml(item.media_url)}" alt="${ns.escapeHtml(item.filename || item.id)}">
          </button>
          ${ns.splitLinkControl("Media Link", item.media_url, "Media URL")}
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
})();
