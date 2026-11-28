"use strict";

// Simplified, integer-only room physics for schematic maps.  The server owns
// state, revision checks and the topology graph; this file only normalizes
// decisions, draws the overlay and posts the existing set/tick/reset actions.
(function (root) {
  const NS = "http://www.w3.org/2000/svg";
  const FIELDS = ["water", "fire", "gas", "heat", "pressure"];
  const COLORS = { water: "#48a8ff", fire: "#ff5848", gas: "#55d68b", heat: "#ffb84d", pressure: "#b58cff" };

  function clamp(value, lo, hi) {
    value = Number(value);
    if (!Number.isFinite(value)) return lo;
    return Math.max(lo, Math.min(hi, Math.trunc(value)));
  }

  function normalizeRoom(value) {
    const src = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const out = {};
    FIELDS.forEach(function (field) {
      out[field] = clamp(src[field], 0, 100);
    });
    return out;
  }

  function normalizeState(value) {
    const src = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const rooms = {};
    const rawRooms = src.rooms && typeof src.rooms === "object" && !Array.isArray(src.rooms) ? src.rooms : {};
    Object.keys(rawRooms).sort().forEach(function (id) {
      rooms[id] = normalizeRoom(rawRooms[id]);
    });
    const graph = src.graph && typeof src.graph === "object" ? src.graph : {};
    return {
      version: 1,
      enabled: src.enabled === true,
      revision: clamp(src.revision, 0, 2147483647),
      tick: clamp(src.tick, 0, 2147483647),
      rooms,
      graph: {
        nodes: Array.isArray(graph.nodes) ? graph.nodes : [],
        edges: Array.isArray(graph.edges) ? graph.edges : [],
      },
    };
  }

  function normalizeSteps(value) {
    const n = typeof value === "number" ? value : Number(value);
    if (!Number.isInteger(n) || n < 1 || n > 16) throw new Error("steps must be between 1 and 16");
    return n;
  }

  function tickState(input, steps) {
    const state = normalizeState(input);
    const bounded = normalizeSteps(steps);
    const graph = state.graph;
    for (let repeat = 0; repeat < bounded; repeat++) {
      FIELDS.forEach(function (field) {
        const diff = {};
        Object.keys(state.rooms).forEach(function (id) { diff[id] = 0; });
        const divisor = field === "fire" ? 6 : 4;
        graph.edges.forEach(function (edge) {
          if (!edge || edge.open === false) return;
          const a = edge.a || edge.from;
          const b = edge.b || edge.to;
          if (!state.rooms[a] || !state.rooms[b]) return;
          const delta = Math.trunc((state.rooms[a][field] - state.rooms[b][field]) / divisor);
          if (delta) {
            diff[a] -= delta;
            diff[b] += delta;
          }
        });
        if (field === "fire") {
          Object.keys(state.rooms).forEach(function (id) {
            if (state.rooms[id].fire > 0) diff[id] -= 2;
          });
        }
        Object.keys(state.rooms).forEach(function (id) {
          state.rooms[id][field] = clamp(state.rooms[id][field] + (diff[id] || 0), 0, 100);
        });
      });
      state.tick = clamp(state.tick + 1, 0, 2147483647);
    }
    state.revision = clamp(state.revision + 1, 0, 2147483647);
    return state;
  }

  function ensureLayer(svg) {
    if (!svg) return null;
    let layer = svg.querySelector("#physics-layer");
    if (!layer) {
      const ns = svg.namespaceURI || NS;
      layer = svg.ownerDocument.createElementNS(ns, "g");
      layer.setAttribute("id", "physics-layer");
      layer.setAttribute("pointer-events", "none");
      const token = svg.querySelector("#token-layer");
      svg.insertBefore(layer, token || null);
    }
    return layer;
  }

  function applyOverlay(svg, state, options) {
    const opts = options || {};
    const layer = ensureLayer(svg);
    if (!layer) return;
    layer.textContent = "";
    state = normalizeState(state);
    if (!state.enabled && !opts.force) return;
    const nodes = state.graph.nodes || [];
    nodes.forEach(function (node) {
      if (!node || !node.id) return;
      const room = state.rooms[String(node.id)];
      if (!room) return;
      let bounds = node.bounds;
      if (!Array.isArray(bounds) || bounds.length < 4) {
        bounds = [node.x, node.y, node.w, node.h];
      }
      const x = Number(bounds[0]), y = Number(bounds[1]), w = Number(bounds[2]), h = Number(bounds[3]);
      if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return;
      let dominant = "pressure";
      let max = -1;
      FIELDS.forEach(function (field) {
        if (room[field] > max) { max = room[field]; dominant = field; }
      });
      if (max <= 0) return;
      const opacity = Math.min(0.68, 0.12 + max / 180);
      const rect = svg.ownerDocument.createElementNS(NS, "rect");
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(w));
      rect.setAttribute("height", String(h));
      rect.setAttribute("fill", COLORS[dominant]);
      rect.setAttribute("opacity", String(opacity));
      rect.setAttribute("stroke", COLORS[dominant]);
      rect.setAttribute("stroke-width", "3");
      rect.setAttribute("rx", "5");
      rect.setAttribute("pointer-events", "none");
      layer.appendChild(rect);
      if (!opts.player && w >= 55 && h >= 28) {
        const text = svg.ownerDocument.createElementNS(NS, "text");
        text.setAttribute("x", String(x + 7));
        text.setAttribute("y", String(y + 17));
        text.setAttribute("fill", "#fff");
        text.setAttribute("font-size", "12");
        text.setAttribute("font-family", "system-ui, sans-serif");
        text.setAttribute("paint-order", "stroke");
        text.setAttribute("stroke", "#111");
        text.setAttribute("stroke-width", "3");
        text.setAttribute("pointer-events", "none");
        const label = node.name ? String(node.name) + " · " : "";
        text.textContent = label + FIELDS.filter(function (field) { return room[field] > 0; })
          .map(function (field) { return field[0].toUpperCase() + "=" + room[field]; }).join(" ");
        layer.appendChild(text);
      }
    });
  }

  async function responseError(response) {
    let detail = "";
    try {
      const data = await response.json();
      detail = data.detail || data.error || "";
    } catch (_) {}
    return detail || ("HTTP " + response.status);
  }

  function initGM(options) {
    const opts = options || {};
    const slug = opts.slug;
    const svg = opts.svg || (typeof document !== "undefined" ? document.getElementById("sch-svg") : null);
    if (!slug) return;
    const enabled = typeof document !== "undefined" ? document.getElementById("physics-enabled") : null;
    const visible = typeof document !== "undefined" ? document.getElementById("physics-visible") : null;
    const roomSelect = typeof document !== "undefined" ? document.getElementById("physics-room") : null;
    const status = typeof document !== "undefined" ? document.getElementById("physics-status") : null;
    let state = normalizeState(null);

    function say(text) {
      if (status) status.textContent = text || "";
    }

    async function load() {
      try {
        const response = await fetch("/maps/schematic/" + encodeURIComponent(slug) + "/physics.json", { credentials: "same-origin" });
        if (!response.ok) throw new Error(await responseError(response));
        state = normalizeState(await response.json());
        if (enabled) enabled.checked = state.enabled;
        if (roomSelect) {
          const old = roomSelect.value;
          roomSelect.textContent = "";
          (state.graph.nodes || []).forEach(function (node) {
            const option = document.createElement("option");
            option.value = String(node.id);
            option.textContent = node.name ? String(node.name) : ("Room " + node.id);
            roomSelect.appendChild(option);
          });
          if (old) roomSelect.value = old;
        }
        if (roomSelect) setRoomInputs(roomSelect.value);
        if (visible && visible.checked) applyOverlay(svg, state, { force: true });
        else if (svg) applyOverlay(svg, state, { force: false });
        say("");
      } catch (error) {
        say(error.message || "Could not load physics.");
      }
    }

    function setRoomInputs(id) {
      const room = state.rooms[String(id)];
      FIELDS.forEach(function (field) {
        const input = document.getElementById("physics-" + field);
        if (input) input.value = room ? String(room[field]) : "0";
      });
    }

    async function postAction(action, extra) {
      const body = Object.assign({ action: action, revision: state.revision }, extra || {});
      try {
        const response = await fetch("/maps/schematic/" + encodeURIComponent(slug) + "/physics", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(body),
        });
        if (!response.ok) throw new Error(await responseError(response));
        state = normalizeState(await response.json());
        if (enabled) enabled.checked = state.enabled;
        if (roomSelect) setRoomInputs(roomSelect.value);
        if (visible && visible.checked) applyOverlay(svg, state, { force: true });
        else applyOverlay(svg, state, { force: false });
        say(action === "tick" ? "Ticked." : "Saved.");
      } catch (error) {
        say(error.message || "Could not save physics.");
      }
    }

    if (enabled) enabled.addEventListener("change", function () {
      postAction("set", { enabled: enabled.checked });
    });
    const tickButton = typeof document !== "undefined" ? document.getElementById("physics-tick") : null;
    if (tickButton) tickButton.addEventListener("click", function () {
      const stepsInput = document.getElementById("physics-steps");
      let steps = 1;
      try { steps = normalizeSteps(stepsInput ? stepsInput.value : 1); }
      catch (error) { say(error.message); return; }
      postAction("tick", { steps: steps });
    });
    const resetButton = typeof document !== "undefined" ? document.getElementById("physics-reset") : null;
    if (resetButton) resetButton.addEventListener("click", function () {
      if (typeof window !== "undefined" && window.confirm && !window.confirm("Reset every room's physics state?")) return;
      postAction("reset", {});
    });
    const setButton = typeof document !== "undefined" ? document.getElementById("physics-set-room") : null;
    if (setButton) setButton.addEventListener("click", function () {
      if (!roomSelect || !roomSelect.value) { say("Choose a room first."); return; }
      const room = {};
      FIELDS.forEach(function (field) {
        const input = document.getElementById("physics-" + field);
        room[field] = clamp(input ? input.value : 0, 0, 100);
      });
      postAction("set", { rooms: { [roomSelect.value]: room } });
    });
    if (roomSelect) roomSelect.addEventListener("change", function () { setRoomInputs(roomSelect.value); });
    if (visible) visible.addEventListener("change", function () {
      if (visible.checked) applyOverlay(svg, state, { force: true });
      else applyOverlay(svg, state, { force: false });
    });
    if (typeof window !== "undefined") {
      window.addEventListener("nd-live", load);
    }
    load();
  }

  root.ndSchematicPhysics = {
    normalizeState,
    normalizeSteps,
    normalizeRoom,
    tickState,
    applyOverlay,
    initGM,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.ndSchematicPhysics;
  }
})(typeof window !== "undefined" ? window : globalThis);
