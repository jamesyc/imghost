window.parseOptionalNumber = (value) => (value === "" ? null : Number(value));
window.parseOptionalDate = (value) => (value === "" ? null : new Date(value).toISOString());

window.escapeAdminHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

window.adminRequestJson = async (url, options = {}) => {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status}).`);
  }
  return data;
};

window.setAdminStatus = (node, message = "", tone = "") => {
  if (!node) {
    return;
  }
  node.textContent = message || "";
  node.classList.toggle("hidden", !message);
  if (tone) {
    node.dataset.tone = tone;
  } else {
    delete node.dataset.tone;
  }
};
