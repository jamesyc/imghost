(() => {
  const ns = window.ImghostAlbumDetail;
  if (!ns) {
    return;
  }

  ns.loadAlbum = async () => {
    if (!ns.state.albumId) {
      ns.setPageStatus("Missing album ID.");
      return;
    }
    ns.setPageStatus("");
    const album = await ns.requestJson(ns.withAlbumAccess(`/api/v1/album/${ns.state.albumId}`));
    ns.renderAlbum(album);
  };

  ns.persistOrder = async (mediaIds) => {
    if (!ns.state.albumId || !mediaIds.length || ns.state.reorderInFlight) {
      return;
    }
    ns.state.reorderInFlight = true;
    try {
      const album = await ns.requestJson(ns.withAlbumAccess(`/api/v1/album/${ns.state.albumId}/order`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mediaIds.map((mediaId, index) => ({
          media_id: mediaId,
          position: (index + 1) * 1000,
        }))),
      });
      ns.renderAlbum(album);
    } catch (error) {
      window.alert(error.message);
      await ns.loadAlbum();
    } finally {
      ns.state.reorderInFlight = false;
    }
  };

  ns.moveItemByOffset = async (mediaId, offset) => {
    const mediaIds = ns.orderedMediaIds();
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
    await ns.persistOrder(reordered);
  };

  ns.persistTitle = async ({ keepEditingOnError = true } = {}) => {
    if (!ns.state.albumId || !ns.state.album || !ns.dom.titleInput || ns.state.titleSaving) {
      return;
    }
    const nextTitle = ns.dom.titleInput.value.trim();
    const currentTitle = ns.state.album.title || "";
    if (nextTitle === currentTitle) {
      ns.setTitleEditing(false);
      return;
    }
    ns.state.titleSaving = true;
    try {
      const album = await ns.requestJson(ns.withAlbumAccess(`/api/v1/album/${ns.state.albumId}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: nextTitle || null }),
      });
      ns.renderAlbum(album);
      ns.setTitleEditing(false);
    } catch (error) {
      window.alert(error.message);
      if (!keepEditingOnError) {
        ns.dom.titleInput.value = ns.state.album.title || "";
        ns.setTitleEditing(false);
      } else {
        ns.setTitleEditing(true);
      }
    } finally {
      ns.state.titleSaving = false;
    }
  };

  ns.persistCover = async (mediaId) => {
    if (!ns.state.albumId) {
      return;
    }
    const album = await ns.requestJson(ns.withAlbumAccess(`/api/v1/album/${ns.state.albumId}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cover_media_id: mediaId }),
    });
    ns.renderAlbum(album);
  };
})();
