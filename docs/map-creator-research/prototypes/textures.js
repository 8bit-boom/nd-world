// Floor, wall and ground textures as SVG patterns and filters: no image files, resolution independent, a few hundred bytes each.
// Patterns are cheap (the browser tiles them); the noise overlay (feTurbulence) is the expensive part - on a real map it would
// be rendered ONCE into a bitmap tile and reused, which is a renderer decision, not an asset one.
(function (root) {
  'use strict';

  function tile(id, size, inner) { return '<pattern id="' + id + '" width="' + size + '" height="' + size + '" patternUnits="userSpaceOnUse">' + inner + '</pattern>'; }

  function defs() {
    var d = '';
    // wooden planks, running north-south, two shades, end joints staggered
    d += '<pattern id="p-wood" width="100" height="200" patternUnits="userSpaceOnUse">' +
      '<rect width="50" height="200" fill="#a8753f"/><rect x="50" width="50" height="200" fill="#9a6a38"/>' +
      '<path d="M0 0V200M50 0V200M100 0V200" stroke="#4a2e17" stroke-width="2.5"/>' +
      '<path d="M0 70H50M50 150H100" stroke="#4a2e17" stroke-width="2"/>' +
      '<path d="M10 20V50M30 100V150M64 30V60M84 110V140" stroke="#7d5229" stroke-width="1.5" opacity=".7"/></pattern>';
    // square stone flags in three shades
    function flags(id, a, b, c, grout) {
      return tile(id, 100, '<rect width="50" height="50" fill="' + a + '"/><rect x="50" width="50" height="50" fill="' + b + '"/><rect y="50" width="50" height="50" fill="' + c + '"/><rect x="50" y="50" width="50" height="50" fill="' + a + '"/>' +
        '<path d="M0 0H100M0 50H100M0 0V100M50 0V100" stroke="' + grout + '" stroke-width="2.5"/>');
    }
    d += flags('p-stone', '#6b6f78', '#62666e', '#70747d', '#383b41');
    d += flags('p-corridor', '#4f535b', '#494d54', '#555961', '#2b2d32');
    d += flags('p-marble', '#cfd0d6', '#c3c5cc', '#d6d7dc', '#8d8f99');
    d += flags('p-dirt', '#6a5236', '#624b31', '#705a3c', '#46351f');
    // wall: running-bond blocks
    d += tile('p-wallstone', 60, '<rect width="60" height="60" fill="#8d9199"/><path d="M0 0H60M0 30H60M0 60H60M30 0V30M0 30V60M60 30V60" stroke="#40434a" stroke-width="3"/><path d="M6 8H24M36 38H54" stroke="#aeb2b9" stroke-width="2" opacity=".7"/>');
    // noise to roughen everything a little: grayscale fractal noise used as a multiply overlay
    d += '<filter id="f-noise" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.018" numOctaves="3" seed="7" result="n"/>' +
      '<feColorMatrix type="matrix" values="0 0 0 0 .5  0 0 0 0 .5  0 0 0 0 .5  .9 .9 .9 0 -.6" result="a"/><feComponentTransfer><feFuncA type="linear" slope="1.1" intercept="0"/></feComponentTransfer></filter>';
    // grass and water as turbulence-driven fills (used for outdoor / cave maps)
    d += '<filter id="f-grass" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.06" numOctaves="4" seed="3"/><feColorMatrix type="matrix" values=".12 0 0 0 .13  0 .25 0 0 .27  0 0 0 0 .10  0 0 0 0 1"/></filter>';
    d += '<filter id="f-water" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.012 0.03" numOctaves="3" seed="11"/><feColorMatrix type="matrix" values="0 0 0 0 .08  0 .15 0 0 .26  0 0 .25 0 .42  0 0 0 0 1"/></filter>';
    d += '<filter id="f-cave" color-interpolation-filters="sRGB" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="4" seed="5"/><feColorMatrix type="matrix" values=".12 0 0 0 .20  0 .10 0 0 .16  0 0 .06 0 .11  0 0 0 0 1"/></filter>';
    return '<defs>' + d + '</defs>';
  }

  root.ndMapTextures = { defs: defs, FLOORS: ['wood', 'stone', 'marble', 'dirt'] };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.ndMapTextures;
})(typeof window !== 'undefined' ? window : globalThis);
