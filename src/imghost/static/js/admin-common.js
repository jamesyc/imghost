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

window.adminFormatNumber = (value) => new Intl.NumberFormat().format(Number(value || 0));

window.adminFormatBytes = (value) => {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unitIndex = 0;
  let size = bytes;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const precision = size >= 10 || unitIndex === 0 ? 0 : 1;
  return `${size.toFixed(precision)} ${units[unitIndex]}`;
};

window.adminFormatDateTime = (value) => {
  if (!value) {
    return "Not recorded";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString();
};
