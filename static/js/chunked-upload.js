"use strict";
// Shared "upload a possibly-huge file" helper. A big file (a whole session
// recording, a long voice-memo attachment) can be blocked by a reverse
// proxy/CDN's own per-request body cap even once this app's own size limit
// is raised — Cloudflare's free tier is a fixed 100 MB with no way to raise
// it (see docs/DEPLOYMENT.md's "Upload size limit" section). ndChunkedUpload
// works around that exactly the way the Audio Library already does
// (app/templates/audio_library.html's audioUploadChunked, the original,
// one-off version of this): split anything over the threshold into
// sub-100MB parts and upload them sequentially, then ask the server to
// reassemble them — a file at or under the threshold just goes straight to
// `directUrl` in one request, same as before this helper existed.
const ND_CHUNK_UPLOAD_THRESHOLD = 100 * 1024 * 1024;
const ND_CHUNK_SIZE = 80 * 1024 * 1024; // safely under the 100MB cap, leaves headroom for multipart overhead

function ndChunkUploadRandomId() {
  const bytes = new Uint8Array(16);
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function ndChunkUploadJsonOrThrow(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
  return data;
}

// opts:
//   directUrl   — where a file at/under the threshold is posted in one request
//   chunkUrl    — receives each part (upload_id, chunk_index, file)
//   completeUrl — reassembles the parts (upload_id, filename, total_chunks, ...extraFields)
//   extraFields — plain object of extra form fields sent alongside the file
//                 on the direct request, or alongside the metadata on the
//                 complete request (never resent on every /chunk request)
//   onProgress  — optional (chunkIndex, totalChunks) callback while chunking
// Returns the parsed JSON body of whichever request actually finished the
// upload (directUrl's response, or completeUrl's).
async function ndChunkedUpload(file, opts) {
  const extraFields = opts.extraFields || {};
  if (file.size <= ND_CHUNK_UPLOAD_THRESHOLD) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    for (const k in extraFields) fd.append(k, extraFields[k]);
    const res = await fetch(opts.directUrl, { method: "POST", body: fd });
    return ndChunkUploadJsonOrThrow(res);
  }

  const uploadId = ndChunkUploadRandomId();
  const totalChunks = Math.ceil(file.size / ND_CHUNK_SIZE);
  for (let idx = 0; idx < totalChunks; idx++) {
    if (opts.onProgress) opts.onProgress(idx, totalChunks);
    const fd = new FormData();
    fd.append("upload_id", uploadId);
    fd.append("chunk_index", String(idx));
    fd.append("file", file.slice(idx * ND_CHUNK_SIZE, (idx + 1) * ND_CHUNK_SIZE));
    const res = await fetch(opts.chunkUrl, { method: "POST", body: fd });
    await ndChunkUploadJsonOrThrow(res);
  }

  if (opts.onProgress) opts.onProgress(totalChunks, totalChunks);
  const fd = new FormData();
  fd.append("upload_id", uploadId);
  fd.append("filename", file.name);
  fd.append("total_chunks", String(totalChunks));
  for (const k in extraFields) fd.append(k, extraFields[k]);
  const res = await fetch(opts.completeUrl, { method: "POST", body: fd });
  return ndChunkUploadJsonOrThrow(res);
}
