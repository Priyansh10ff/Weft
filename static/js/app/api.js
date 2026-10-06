// Thin client over the Weft HTTP API. Every call rejects with an Error whose
// message is the server's `detail` when there is one.

async function request(path, { method = "GET", body, signal } = {}) {
  const init = { method, signal, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("Can't reach the Weft server. Is it running?");
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!response.ok) {
    const detail = data && typeof data === "object" ? data.detail : null;
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join("; ")
      : detail || `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

export const api = {
  health: () => request("/health"),
  stats: () => request("/stats"),
  sources: () => request("/sources"),
  source: (id) => request(`/sources/${encodeURIComponent(id)}`),
  deleteSource: (id) => request(`/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),
  segment: (id) => request(`/segments/${encodeURIComponent(id)}`),
  entities: (q, limit = 200) => request(`/entities?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  entity: (id) => request(`/entities/${encodeURIComponent(id)}`),
  entityTimeline: (id) => request(`/entities/${encodeURIComponent(id)}/timeline`),
  graph: (id, depth = 1) => request(`/graph/${encodeURIComponent(id)}?depth=${depth}`),
  job: (id) => request(`/jobs/${encodeURIComponent(id)}`),
  query: (query, limit = 8, signal) => request("/query", { method: "POST", body: { query, limit }, signal }),
  compare: (query, limit = 8, signal) => request("/query/compare", { method: "POST", body: { query, limit }, signal }),
  seedDemo: () => request("/demo/seed", { method: "POST" }),
  evalLatest: () => request("/eval/latest"),
  evalRun: ({ dataset = "demo", k = 5, systems } = {}) => request("/eval/run", { method: "POST", body: { dataset, k, ...(systems ? { systems } : {}) } }),
};

export const UPLOAD_ROUTES = {
  ".mp4": "video",
  ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio", ".ogg": "audio",
  ".png": "image", ".jpg": "image", ".jpeg": "image",
  ".pdf": "pdf",
  ".json": "json", ".txt": "json",
};

export function modalityFor(filename) {
  const dot = filename.lastIndexOf(".");
  return dot === -1 ? null : UPLOAD_ROUTES[filename.slice(dot).toLowerCase()] || null;
}

/**
 * Upload one file as a background job.
 * Calls onProgress(fraction) while bytes are sent; resolves with the upload response.
 */
export function uploadFile(file, { onProgress, force = false } = {}) {
  const modality = modalityFor(file.name);
  if (!modality) return Promise.reject(new Error(`Unsupported file type: ${file.name}`));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const params = new URLSearchParams({ background: "true" });
    if (force) params.set("force", "true");
    xhr.open("POST", `/upload/${modality}?${params}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new Error("Upload failed: the server could not be reached."));
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error((data && data.detail) || `Upload failed (${xhr.status})`));
    };
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
