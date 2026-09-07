// Shared "poll on an interval, but skip while the tab is hidden" helper —
// see docs/AUDIT_PLAN_NEXT.md item 13 (base.html's spotlight/now-playing
// poller and schematic_view.html's battle-map poller both used a bare
// setInterval that kept firing at full cadence in a backgrounded tab, for
// state nobody was looking at). `fn` runs once immediately, then again on
// every tick where the tab is visible — a tick while hidden is skipped
// rather than queued — and once more immediately when the tab becomes
// visible again, so returning to a backgrounded tab re-syncs right away
// instead of waiting up to `ms` more.
function ndPoll(fn, ms) {
  fn();
  setInterval(() => { if (!document.hidden) fn(); }, ms);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fn();
  });
}
