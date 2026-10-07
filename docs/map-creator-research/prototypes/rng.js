// Seeded random numbers: the same seed gives the same map, which is what makes generators testable.
// mulberry32 is a small, well-known 32-bit generator; it is not cryptographic and does not need to be.
(function (root) {
  'use strict';

  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // A string seed ("smugglers-den") becomes a number, so a GM can type a word instead of a digit string.
  function hashSeed(s) {
    var h = 2166136261 >>> 0;
    s = String(s);
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  }

  function makeRng(seed) {
    var next = mulberry32(typeof seed === 'number' ? seed : hashSeed(seed));
    return {
      next: next,
      int: function (lo, hi) { return lo + Math.floor(next() * (hi - lo + 1)); },      // inclusive both ends
      chance: function (p) { return next() < p; },
      pick: function (arr) { return arr[Math.floor(next() * arr.length)]; },
      shuffle: function (arr) {
        var a = arr.slice();
        for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(next() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t; }
        return a;
      },
    };
  }

  root.ndMapRng = { makeRng: makeRng, hashSeed: hashSeed, mulberry32: mulberry32 };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapRng;
})(typeof window !== 'undefined' ? window : globalThis);
