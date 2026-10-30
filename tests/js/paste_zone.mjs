// Ctrl+V of a picture into the drop zones (static/js/file-dropzone.js), in a real Chromium. Run by tests/test_paste_zone_js.py (skipped without node + playwright).
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");   // CommonJS lookup, so a global install (NODE_PATH) is found
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent(`<!doctype html><html><body>
  <div id="z1" class="form-dropzone" data-dropzone><div class="form-dropzone-hint">Choose a file, or drag &amp; drop an image here</div><input type="file" id="f1" accept="image/*"></div>
  <div id="z2" class="form-dropzone" data-dropzone><div class="form-dropzone-hint">Pick pictures</div><input type="file" id="f2" accept="image/*" multiple></div>
  <div id="z3" class="form-dropzone" data-dropzone><div class="form-dropzone-hint">JSON files</div><input type="file" id="f3" accept=".json"></div>
  <input type="text" id="t1"><textarea id="ta"></textarea>
  <div id="custom" style="width:80px;height:30px;background:#ccc">custom</div>
</body></html>`);
await page.addScriptTag({ path: path.join(root, "static", "js", "file-dropzone.js") });
await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
await page.evaluate(() => {
  window.changes = [];
  for (const id of ["f1", "f2", "f3"]) document.getElementById(id).addEventListener("change", () => window.changes.push(id));
});

// a paste event carrying files and / or text; returns whether the page took it over (preventDefault)
const paste = (target, files, text) => page.evaluate(({ target, files, text }) => {
  const dt = new DataTransfer();
  for (const f of files) dt.items.add(new File([new Uint8Array([137, 80, 78, 71])], f.name, { type: f.type }));
  if (text) dt.setData("text/plain", text);
  const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
  (target ? document.querySelector(target) : document.body).dispatchEvent(ev);
  return ev.defaultPrevented;
}, { target, files, text });
const state = () => page.evaluate(() => ({
  f1: [...document.getElementById("f1").files].map((f) => f.name), f2: [...document.getElementById("f2").files].map((f) => f.name),
  f3: [...document.getElementById("f3").files].map((f) => f.name), changes: window.changes.slice(),
}));
const fails = [];
const check = (name, ok, extra) => { if (!ok) fails.push(name + (extra ? " " + JSON.stringify(extra) : "")); };
const png = (name = "image.png") => ({ name, type: "image/png" });

// 1. two image zones and nothing hovered or focused: the paste is not guessed at
let took = await paste(null, [png()]);
let s = await state();
check("no guess between two zones", !took && !s.f1.length && !s.f2.length, s);

// 2. the hint says paste works
const hints = await page.evaluate(() => [...document.querySelectorAll(".form-dropzone-hint")].map((h) => h.textContent));
check("hint 1 mentions Ctrl+V", /Ctrl\+V/.test(hints[0]), hints);
check("hint 2 mentions Ctrl+V", /Ctrl\+V/.test(hints[1]), hints);
check("a JSON-only zone gets no paste hint", !/Ctrl\+V/.test(hints[2]), hints);

// 3. the zone under the cursor takes it: one file in a single-file input, named by the paste time
await page.hover("#z1");
took = await paste(null, [png(), png()]);
s = await state();
check("hovered zone takes the paste", took && s.f1.length === 1 && /^pasted-\d{8}-\d{6}\.png$/.test(s.f1[0]) && s.changes.includes("f1"), s);
check("other zones untouched", !s.f2.length && !s.f3.length, s);

// 4. a multiple input keeps every picture, a name the browser gave is kept
await page.hover("#z2");
took = await paste(null, [png(), { name: "sheet.jpg", type: "image/jpeg" }]);
s = await state();
check("multiple input gets both", took && s.f2.length === 2 && s.f2[1] === "sheet.jpg" && /\.png$/.test(s.f2[0]), s);

// 5. a paste inside a text field with text on the clipboard stays an ordinary paste
await page.hover("#z1");
await page.focus("#t1");
before: {
  const prior = (await state()).changes.length;
  took = await paste("#t1", [png()], "https://example.org/pic.png");
  check("text field with text: left alone", !took && (await state()).changes.length === prior);
}
// 6. text only: left alone
took = await paste(null, [], "just text");
check("text only: left alone", !took);
// 7. a paste with a non-image file: left alone
took = await paste(null, [{ name: "a.pdf", type: "application/pdf" }]);
check("a pdf is not an image", !took);
// 8. a picture pasted while the focus is in a file input of a zone goes to that zone
await page.focus("#f2");
await page.mouse.move(500, 500);
await page.evaluate(() => { document.getElementById("f2").value = ""; });
took = await paste("#f2", [png("focus.png")]);
s = await state();
check("focus inside a zone", took && s.f2.includes("focus.png"), s);

// 9. a page's own zone joins with ndRegisterPasteZone
await page.evaluate(() => { window.got = null; ndRegisterPasteZone(document.getElementById("custom"), (files) => { window.got = files.map((f) => f.name + ":" + (f instanceof File)); }); });
await page.hover("#custom");
took = await paste(null, [png("c.png")]);
const got = await page.evaluate(() => window.got);
check("custom zone gets an array of Files", took && JSON.stringify(got) === JSON.stringify(["c.png:true"]), got);

await browser.close();
if (fails.length) { console.error("FAILED:\n" + fails.join("\n")); process.exit(1); }
console.log("paste ok");
