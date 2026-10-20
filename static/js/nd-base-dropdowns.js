// Shared open/close for both dropdown types (world switcher, per-menu tools
// switcher) — mutual exclusion (opening one closes any other), Escape to
// close with focus returned to the trigger button, and aria-expanded kept
// in sync with the .open class on every path (click-toggle, click-outside,
// Escape) so the three never drift apart.
function ndCloseDropdowns(returnFocus) {
  var opened = document.querySelector('.world-switcher.open, .tools-switcher.open');
  var btn = opened ? opened.querySelector('.world-btn, .tools-btn') : null;
  document.querySelectorAll('.world-switcher.open, .tools-switcher.open').forEach(function(el) {
    el.classList.remove('open');
    var b = el.querySelector('.world-btn, .tools-btn');
    if (b) b.setAttribute('aria-expanded', 'false');
  });
  if (returnFocus && btn) btn.focus();
}
function ndToggleDropdown(btn) {
  var wrap = btn.parentElement;
  var willOpen = !wrap.classList.contains('open');
  ndCloseDropdowns(false);
  if (willOpen) {
    wrap.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
  }
}
document.addEventListener('click', e => {
  if (!e.target.closest('.world-switcher') && !e.target.closest('.tools-switcher')) {
    ndCloseDropdowns(false);
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') ndCloseDropdowns(true);
});

// [spoiler]...[/spoiler] (render_md/_apply_inline_styles) reveal-on-click —
// delegated so it works for every .spoiler-text on the page, including ones
// added later (e.g. a note edited in place) without re-binding anything.
// Space/Enter also reveal it since the span is tabindex="0" role="button"
// (a real player using a screen reader or keyboard-only navigation still
// needs a way to trigger it).
document.addEventListener('click', e => {
  var sp = e.target.closest('.spoiler-text');
  if (sp) sp.classList.add('revealed');
});
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('spoiler-text')) {
    e.preventDefault();
    e.target.classList.add('revealed');
  }
});
