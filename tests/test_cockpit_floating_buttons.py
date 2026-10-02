"""The GM's two floating round buttons — 📺 second screen (bottom-left, nd-stage.js) and 🐞 activity log (bottom-right,
nd-logger.js) — were pinned 14px from the bottom of every page. On the cockpit that is exactly where the status bar is, so
the 🐞 sat on the saved label and the ⛶ fullscreen button, and the 📺 on the window count. On a phone they hid the
bottom-right corner of the panel (the AI chat's send button) and the cockpit's own tab bar was pushed below the fold by the
site's top bar, so there was nothing to lift them over.

The buttons now position themselves from one variable, `--nd-fab-bottom`; the cockpit raises it above its status bar and
hides the buttons in its phone/tablet shell; and that shell is sized to the space under the top bar like the desktop one."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGE = (ROOT / "static" / "js" / "nd-stage.js").read_text()
LOGGER = (ROOT / "static" / "js" / "nd-logger.js").read_text()
COCKPIT_HTML = (ROOT / "app" / "templates" / "cockpit.html").read_text()
COCKPIT_JS = (ROOT / "static" / "js" / "cockpit.js").read_text()


def test_both_buttons_take_their_height_from_one_variable():
    for name, src in (("nd-stage.js", STAGE), ("nd-logger.js", LOGGER)):
        assert "--nd-fab-bottom" in src, name
        # nothing may pin the button (or the panel it opens) to a fixed offset again
        assert not re.search(r"bottom:\s*14px", src), f"{name} still pins a button 14px from the bottom"
        assert not re.search(r"bottom:\s*(60|64)px", src), f"{name} still pins its panel to a fixed offset"
    # the panels open above their button, whatever height that is
    assert re.search(r"#nd-stage-panel\{[^}]*bottom:calc\(var\(--nd-fab-bottom", STAGE)
    assert re.search(r'panel\.style\.cssText = "position:fixed;bottom:calc\(var\(--nd-fab-bottom', LOGGER)


def test_the_variable_defaults_to_the_old_spot_everywhere_else():
    # a page that never sets it looks exactly as before
    assert "var(--nd-fab-bottom,14px)" in STAGE
    assert "var(--nd-fab-bottom,14px)" in LOGGER


def test_cockpit_lifts_the_buttons_above_its_status_bar_and_clears_them_on_phones():
    css = COCKPIT_HTML
    m = re.search(r"body:has\(#ck-wrap\)\s*\{\s*--nd-fab-bottom:\s*([0-9.]+)rem", css)
    assert m, "the desktop cockpit must raise --nd-fab-bottom (in rem, so it follows the UI scale)"
    # the status bar is ~1.75rem tall (.25rem padding either side of a .85rem button): clear it with room to spare
    assert float(m.group(1)) >= 2.4
    # the phone/tablet shell is full-bleed panels with a tab bar: no floating buttons there
    assert re.search(
        r"body\.ck-mobile\s+#nd-stage-fab[^{]*#nd-logger-toggle[^{]*\{[^}]*display:\s*none\s*!important", css
    ) or re.search(r"body\.ck-mobile\s+#nd-logger-toggle[^{]*#nd-stage-fab[^{]*\{[^}]*display:\s*none\s*!important", css)
    for ident in ("#nd-stage-panel", "#nd-logger-panel"):
        assert re.search(r"body\.ck-mobile[^{]*" + re.escape(ident) + r"[^{]*\{[^}]*display:\s*none\s*!important", css), ident


def test_toasts_stack_above_the_bug_button_not_under_it():
    m = re.search(r"#ck-toasts\s*\{[^}]*bottom:\s*([0-9.]+)px", COCKPIT_HTML)
    assert m and int(float(m.group(1))) >= 90


def test_mobile_shell_is_sized_to_the_space_under_the_top_bar():
    # fitViewport() sized only the desktop wrap, so the phone shell was a full viewport tall BELOW the top bar and its
    # tab bar sat off-screen until the page was scrolled
    body = re.search(r"function fitViewport\(\) \{(.*?)\n  \}\n", COCKPIT_JS, re.S)
    assert body, "fitViewport not found"
    assert "ck-mwrap" in body.group(1)
    # sized whether or not it is the visible shell, so a rotate that flips between the two needs no second pass
    assert "window.addEventListener('resize', function () { fitViewport(); syncMode(); });" in COCKPIT_JS
