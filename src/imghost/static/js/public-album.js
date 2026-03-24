const publicAlbumRoot = document.getElementById("public-album-items");
const publicAlbumBootstrapNode = document.getElementById("public-album-bootstrap");
const publicAlbumHeroActions = document.getElementById("public-album-hero-actions");
const publicAlbumBootstrap = publicAlbumBootstrapNode ? JSON.parse(publicAlbumBootstrapNode.textContent || "{}") : {};
const publicAlbumId = publicAlbumBootstrap.id || null;
const CLIENT_COMPAT_WARNING_RULES = {
  hevc: {
    canPlayType: 'video/mp4; codecs="hev1"',
    message: "This video uses HEVC encoding and may not play in Firefox. Try Chrome or Safari.",
  },
  vp9_webm: {
    canPlayType: 'video/webm; codecs="vp9"',
    message: "This video may not play in older Safari. Try Chrome or Firefox.",
  },
};

const ensurePublicAlbumLightbox = () => {
  let lightbox = document.getElementById("public-album-lightbox");
  if (lightbox) {
    return lightbox;
  }
  lightbox = document.createElement("div");
  lightbox.id = "public-album-lightbox";
  lightbox.className = "public-album-lightbox hidden";
  lightbox.innerHTML = `
    <button type="button" class="public-album-lightbox-backdrop" aria-label="Close preview"></button>
    <div class="public-album-lightbox-dialog" role="dialog" aria-modal="true" aria-label="Public album preview">
      <button type="button" class="public-album-lightbox-close" aria-label="Close preview">Close</button>
      <img class="public-album-lightbox-image hidden" alt="">
      <video class="public-album-lightbox-video hidden" controls preload="metadata"></video>
    </div>
  `;
  document.body.append(lightbox);
  return lightbox;
};

const closePublicAlbumLightbox = () => {
  const lightbox = document.getElementById("public-album-lightbox");
  if (!lightbox) {
    return;
  }
  lightbox.classList.add("hidden");
  document.body.classList.remove("lightbox-open");
  const image = lightbox.querySelector(".public-album-lightbox-image");
  const video = lightbox.querySelector(".public-album-lightbox-video");
  if (image) {
    image.src = "";
    image.classList.add("hidden");
  }
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
    video.classList.add("hidden");
  }
};

const openPublicAlbumLightbox = ({ mediaUrl, mediaType, filename }) => {
  const lightbox = ensurePublicAlbumLightbox();
  const image = lightbox.querySelector(".public-album-lightbox-image");
  const video = lightbox.querySelector(".public-album-lightbox-video");
  if (!image || !video) {
    return;
  }
  if (mediaType === "video") {
    image.classList.add("hidden");
    video.src = mediaUrl;
    video.classList.remove("hidden");
    video.focus();
  } else {
    video.classList.add("hidden");
    image.src = mediaUrl;
    image.alt = filename || "Public album media";
    image.classList.remove("hidden");
  }
  lightbox.classList.remove("hidden");
  document.body.classList.add("lightbox-open");
};

const pollThumb = (button) => {
  const thumbSrc = button.dataset.thumbSrc;
  const placeholder = button.querySelector("[data-thumb-status]");
  if (!thumbSrc || !placeholder) {
    return;
  }
  const poll = async () => {
    try {
      const response = await fetch(thumbSrc, { method: "GET", cache: "no-store" });
      if (response.status === 200) {
        const image = document.createElement("img");
        image.src = thumbSrc;
        image.alt = button.dataset.filename || "Album media";
        placeholder.replaceWith(image);
        return;
      }
      if (response.status === 202) {
        window.setTimeout(poll, 1000);
        return;
      }
      placeholder.textContent = "Thumbnail failed";
      placeholder.removeAttribute("data-thumb-status");
    } catch {
      window.setTimeout(poll, 1500);
    }
  };
  poll();
};

publicAlbumRoot?.querySelectorAll(".public-album-preview[data-thumb-status]").forEach((button) => {
  pollThumb(button);
});

publicAlbumRoot?.addEventListener("click", (event) => {
  const previewButton = event.target.closest(".public-album-preview");
  if (!previewButton) {
    return;
  }
  openPublicAlbumLightbox({
    mediaUrl: previewButton.dataset.mediaUrl,
    mediaType: previewButton.dataset.mediaType,
    filename: previewButton.dataset.filename,
  });
});

const injectManageButton = () => {
  if (!publicAlbumHeroActions || !publicAlbumId) {
    return;
  }
  const access = window.imghostAnonAlbums?.read?.(publicAlbumId);
  if (!access?.manageUrl || publicAlbumHeroActions.querySelector(".public-album-manage-link")) {
    return;
  }
  const manageLink = document.createElement("a");
  manageLink.className = "button-link secondary-link public-album-manage-link";
  manageLink.href = access.manageUrl;
  manageLink.textContent = "Manage Album";
  publicAlbumHeroActions.append(manageLink);
};

injectManageButton();

const renderClientCompatibilityWarnings = () => {
  if (!publicAlbumRoot) {
    return;
  }
  const probeVideo = document.createElement("video");
  for (const item of publicAlbumBootstrap.items || []) {
    const warningKey = item.client_compat_check;
    if (!warningKey) {
      continue;
    }
    const warningRule = CLIENT_COMPAT_WARNING_RULES[warningKey];
    if (!warningRule) {
      continue;
    }
    const existingWarning = publicAlbumRoot.querySelector(
      `[data-media-id="${item.id}"] [data-warning-key="${warningKey}"]:not([data-client-compat-warning])`,
    );
    if (existingWarning) {
      continue;
    }
    if (probeVideo.canPlayType(warningRule.canPlayType) !== "") {
      continue;
    }
    const placeholder = publicAlbumRoot.querySelector(
      `[data-client-compat-warning][data-media-id="${item.id}"][data-warning-key="${warningKey}"]`,
    );
    if (!placeholder) {
      continue;
    }
    placeholder.textContent = warningRule.message;
    placeholder.classList.remove("hidden");
  }
};

renderClientCompatibilityWarnings();

document.addEventListener("click", (event) => {
  if (event.target.closest(".public-album-lightbox-backdrop, .public-album-lightbox-close")) {
    closePublicAlbumLightbox();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closePublicAlbumLightbox();
  }
});
