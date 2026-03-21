const albumCardEscapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

const albumCardFormatBytes = (value) => {
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

const albumCardPrivatePath = (albumId) => `/albums/${encodeURIComponent(albumId)}`;
const albumCardPublicUrl = (albumId) => `${window.location.origin}/a/${encodeURIComponent(albumId)}`;

const albumCardPreviewItem = (album) => {
  if (!Array.isArray(album.items) || !album.items.length) {
    return null;
  }
  if (album.cover_media_id) {
    const coverItem = album.items.find((item) => item.id === album.cover_media_id);
    if (coverItem) {
      return coverItem;
    }
  }
  return album.items[0];
};

const albumCardThumbMarkup = (album) => {
  const previewItem = albumCardPreviewItem(album);
  if (!previewItem) {
    return '<span class="dashboard-recent-album-thumb-placeholder">No media</span>';
  }
  if (previewItem.thumb_status === "failed") {
    return '<span class="dashboard-recent-album-thumb-placeholder">Thumbnail failed</span>';
  }
  if (previewItem.thumb_status === "pending" || previewItem.thumb_status === "processing") {
    return '<span class="dashboard-recent-album-thumb-placeholder">Thumbnail pending</span>';
  }
  return `<img src="${albumCardEscapeHtml(previewItem.thumb_url)}" alt="${albumCardEscapeHtml(album.title || "Untitled album")}">`;
};

const albumCardPublicLinkControl = (album) => {
  const publicUrl = albumCardPublicUrl(album.id);
  return `
    <div class="split-link-control">
      <a class="split-link-action" href="${publicUrl}" target="_blank" rel="noreferrer">Public Page</a>
      <input
        class="split-link-value"
        type="text"
        value="${albumCardEscapeHtml(publicUrl)}"
        readonly
        aria-label="Public page URL"
        onclick="this.select()"
      >
    </div>
  `;
};

window.renderAlbumCard = (album, options = {}) => {
  const metaText = options.metaText ? options.metaText(album) : `${album.item_count} item(s)`;
  const actions = options.actions ? options.actions(album) : albumCardPublicLinkControl(album);
  return `
    <section
      class="album-card album-list-card dashboard-recent-album-card"
      role="link"
      tabindex="0"
      data-album-open-url="${albumCardPrivatePath(album.id)}"
      aria-label="Open album ${albumCardEscapeHtml(album.title || album.id)}"
    >
      <div class="dashboard-recent-album-copy">
        <div>
          <h3>${albumCardEscapeHtml(album.title || "Untitled album")}</h3>
          <p class="hint">${metaText}</p>
        </div>
        <div class="row row-actions${options.actionsClass ? ` ${options.actionsClass}` : ""}">
          ${actions}
        </div>
      </div>
      <a class="dashboard-recent-album-thumb" href="${albumCardPrivatePath(album.id)}" aria-label="Open private album ${albumCardEscapeHtml(album.title || album.id)}">
        ${albumCardThumbMarkup(album)}
      </a>
    </section>
  `;
};

window.attachAlbumCardNavigation = (root) => {
  if (!root) {
    return;
  }

  let gesture = null;
  const interactiveTarget = (target) => target.closest("a, input, button, textarea, select, label");

  root.addEventListener("mousedown", (event) => {
    const card = event.target.closest("[data-album-open-url]");
    if (!card) {
      gesture = null;
      return;
    }
    gesture = {
      card,
      x: event.clientX,
      y: event.clientY,
      interactive: Boolean(interactiveTarget(event.target)),
    };
  });

  root.addEventListener("click", (event) => {
    const card = event.target.closest("[data-album-open-url]");
    if (!card) {
      return;
    }
    if (interactiveTarget(event.target)) {
      return;
    }
    const selection = window.getSelection?.().toString().trim();
    if (selection) {
      return;
    }
    if (!gesture || gesture.card !== card || gesture.interactive) {
      return;
    }
    const movedX = Math.abs(event.clientX - gesture.x);
    const movedY = Math.abs(event.clientY - gesture.y);
    if (Math.max(movedX, movedY) > 6) {
      return;
    }
    if (card.dataset.albumOpenUrl) {
      window.location.assign(card.dataset.albumOpenUrl);
    }
  });

  root.addEventListener("keydown", (event) => {
    const card = event.target.closest("[data-album-open-url]");
    if (!card || interactiveTarget(event.target)) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      window.location.assign(card.dataset.albumOpenUrl);
    }
  });
};

window.albumCardFormatBytes = albumCardFormatBytes;
window.albumCardEscapeHtml = albumCardEscapeHtml;
window.albumCardPublicLinkControl = albumCardPublicLinkControl;
