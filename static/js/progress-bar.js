"use strict";
// Small shared progress-bar widget, driven by ndChunkedUpload's onProgress
// callback (chunked-upload.js): a real percent while bytes are uploading,
// an indeterminate animated state once the server is processing (Whisper
// transcription / Ollama summarization / chunk reassembly) with no
// progress signal of its own. Used by the Whisper Test tab, Session audio
// recap, and the AI Chat/Ask AI attachment picker — anywhere audio goes
// through ndChunkedUpload.

// Mounts a fresh bar into `container` (replacing any previous content) and
// returns {setPercent(percent, label), setIndeterminate(label), clear()}.
function ndProgressBar(container) {
  container.innerHTML =
    '<div class="nd-progress-wrap">' +
      '<div class="nd-progress-track"><div class="nd-progress-fill"></div></div>' +
      '<div class="nd-progress-label"></div>' +
    "</div>";
  const fill = container.querySelector(".nd-progress-fill");
  const label = container.querySelector(".nd-progress-label");

  return {
    setPercent(percent, text) {
      fill.classList.remove("nd-progress-fill--indeterminate");
      fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
      label.textContent = text || (percent + "%");
    },
    setIndeterminate(text) {
      fill.classList.add("nd-progress-fill--indeterminate");
      fill.style.width = "";
      label.textContent = text || "Working…";
    },
    clear() {
      container.innerHTML = "";
    },
  };
}

// Adapts a ndChunkedUpload {phase, percent} progress event onto a bar
// created by ndProgressBar, with sensible default labels for the upload vs
// processing phase — the one bit of wiring every call site would otherwise
// repeat identically.
function ndProgressFromUpload(bar, event, processingLabel) {
  if (event.phase === "upload") {
    bar.setPercent(event.percent, "Uploading… " + event.percent + "%");
  } else {
    bar.setIndeterminate(processingLabel || "Processing…");
  }
}
