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
//
// Uses XMLHttpRequest rather than fetch specifically so upload progress is
// observable (fetch has no upload-progress event) — real byte-level percent
// while bytes are still going over the wire, then an explicit "processing"
// phase once the request body has fully arrived and the server is doing
// its (unmeasurable — Whisper transcription/Ollama summarization report no
// progress of their own) work before responding.
const ND_CHUNK_UPLOAD_THRESHOLD = 100 * 1024 * 1024;
const ND_CHUNK_SIZE = 80 * 1024 * 1024; // safely under the 100MB cap, leaves headroom for multipart overhead

function ndChunkUploadRandomId() {
  const bytes = new Uint8Array(16);
  if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// Promise-wrapped XHR POST of a FormData body. onUploadProgress(loaded,
// total) fires repeatedly while the body is being sent (only when the
// browser can report a content-length, i.e. lengthComputable) and once
// more with loaded===total right as the body finishes sending — the caller
// uses that to know when to switch from a real percent to an indeterminate
// "processing" state.
function ndXhrUpload(url, formData, onUploadProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    if (onUploadProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onUploadProgress(e.loaded, e.total);
      };
    }
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* non-JSON error body */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || ("HTTP " + xhr.status)));
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(formData);
  });
}

// opts:
//   directUrl   — where a file at/under the threshold is posted in one request
//   chunkUrl    — receives each part (upload_id, chunk_index, file)
//   completeUrl — reassembles the parts (upload_id, filename, total_chunks, ...extraFields)
//   extraFields — plain object of extra form fields sent alongside the file
//                 on the direct request, or alongside the metadata on the
//                 complete request (never resent on every /chunk request)
//   onProgress  — optional ({phase, percent}) callback. phase is "upload"
//                 (percent 0-100, real bytes-sent progress) or "processing"
//                 (no percent — the request body has fully arrived and the
//                 server is transcribing/summarizing/reassembling with no
//                 progress signal of its own; render this as indeterminate)
// Returns the parsed JSON body of whichever request actually finished the
// upload (directUrl's response, or completeUrl's).
async function ndChunkedUpload(file, opts) {
  const extraFields = opts.extraFields || {};
  const report = (phase, percent) => { if (opts.onProgress) opts.onProgress({ phase, percent }); };

  if (file.size <= ND_CHUNK_UPLOAD_THRESHOLD) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    for (const k in extraFields) fd.append(k, extraFields[k]);
    return ndXhrUpload(opts.directUrl, fd, (loaded, total) => {
      report("upload", Math.round((loaded / total) * 100));
      if (loaded >= total) report("processing");
    });
  }

  const uploadId = ndChunkUploadRandomId();
  const totalChunks = Math.ceil(file.size / ND_CHUNK_SIZE);
  let bytesDoneBeforeThisChunk = 0;
  for (let idx = 0; idx < totalChunks; idx++) {
    const chunkStart = idx * ND_CHUNK_SIZE;
    const chunkBlob = file.slice(chunkStart, chunkStart + ND_CHUNK_SIZE);
    const fd = new FormData();
    fd.append("upload_id", uploadId);
    fd.append("chunk_index", String(idx));
    fd.append("file", chunkBlob);
    await ndXhrUpload(opts.chunkUrl, fd, (loaded) => {
      report("upload", Math.round(((bytesDoneBeforeThisChunk + loaded) / file.size) * 100));
    });
    bytesDoneBeforeThisChunk += chunkBlob.size;
  }

  report("processing");
  const fd = new FormData();
  fd.append("upload_id", uploadId);
  fd.append("filename", file.name);
  fd.append("total_chunks", String(totalChunks));
  for (const k in extraFields) fd.append(k, extraFields[k]);
  return ndXhrUpload(opts.completeUrl, fd);
}
