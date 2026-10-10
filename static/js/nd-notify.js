/* Alerts on the character page: "your turn", "you're up next", a new handout, a changed game night. They come from the strip's
 * own refresh (nd-live + a 30 s check), so they arrive while the page is open - in a background tab or a backgrounded phone
 * browser, as far as the browser keeps it alive. There is no push server: with the browser closed nothing can alert you.
 *
 * The player turns it on with the 🔔 button (the browser only asks for permission after a tap). */
(function () {
  'use strict';
  var core = window.ndNotifyCore, KEY = 'ndNotify:v1';
  var supported = !!core && 'Notification' in window;
  var enabled = function () { try { return supported && Notification.permission === 'granted' && localStorage.getItem(KEY) === '1'; } catch (e) { return false; } };

  function show(a) {
    var opts = { body: a.body, tag: 'nd-' + a.tag, renotify: true, icon: '/favicon.ico', vibrate: a.vibrate };
    var viaWorker = navigator.serviceWorker && navigator.serviceWorker.ready;
    if (viaWorker) {                             // Android Chrome only allows notifications through the service worker
      navigator.serviceWorker.ready.then(function (reg) { return reg.showNotification(a.title, opts); }).catch(function () { try { new Notification(a.title, opts); } catch (e) { /* none */ } });
    } else {
      try { new Notification(a.title, opts); } catch (e) { /* none */ }
    }
  }

  window.ndNotify = {
    supported: supported,
    enabled: enabled,
    state: function () { return !supported ? 'unsupported' : Notification.permission === 'denied' ? 'denied' : enabled() ? 'on' : 'off'; },
    /* called from a tap */
    toggle: function () {
      if (!supported) return Promise.resolve('unsupported');
      if (enabled()) { try { localStorage.setItem(KEY, '0'); } catch (e) { /* ignore */ } return Promise.resolve('off'); }
      return Notification.requestPermission().then(function (p) {
        try { localStorage.setItem(KEY, p === 'granted' ? '1' : '0'); } catch (e) { /* ignore */ }
        if (p === 'granted') show({ tag: 'test', title: '🔔 Alerts are on', body: 'You will be told when it is your turn.', vibrate: [60] });
        return p === 'granted' ? 'on' : p === 'denied' ? 'denied' : 'off';
      });
    },
    /* the strip calls this with each fresh snapshot; alerts only while the page is hidden (visible, the strip itself says it) */
    check: function (prev, next) {
      if (!enabled() || !document.hidden) return;
      core.decide(prev, next).forEach(show);
    },
  };
})();
