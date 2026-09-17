/*
 * Human action recorder.
 *
 * Installed into the SAME live page the automation drives, across every
 * same-origin frame. Capture-phase listeners mean the recorder sees the event
 * before the application's own handlers can stop propagation, so a legacy
 * onclick that navigates away is still recorded.
 *
 * Password field values are never captured, at the source.
 */
(function () {
  var top = window;
  try { top = window.top || window; } catch (e) { top = window; }
  if (!top.__cua_human) top.__cua_human = [];

  function txt(el) {
    if (!el) return "";
    var t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.length > 120 ? t.slice(0, 120) : t;
  }
  function at(el, n) { try { return el.getAttribute(n) || ""; } catch (e) { return ""; } }

  function describe(el) {
    if (!el || !el.tagName) return { role: "unknown", name: "", secret: false };
    var tag = el.tagName.toLowerCase();
    var type = (at(el, "type") || "").toLowerCase();
    var role =
      tag === "a" ? "link"
      : tag === "select" ? "combobox"
      : tag === "button" ? "button"
      : tag === "input"
        ? (type === "submit" || type === "button" || type === "reset" ? "button" : "textbox")
        : (at(el, "onclick") ? "button" : tag);
    var name = at(el, "aria-label");
    if (!name && el.id && el.ownerDocument) {
      var lab = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) name = txt(lab);
    }
    if (!name && tag === "input" && (type === "submit" || type === "button" || type === "reset"))
      name = at(el, "value");
    if (!name && (role === "button" || role === "link")) name = txt(el);
    if (!name) {
      var cell = el.closest ? el.closest("td,th") : null;
      if (cell && cell.previousElementSibling) name = txt(cell.previousElementSibling);
    }
    if (!name) name = at(el, "name") || at(el, "placeholder") || tag;
    return { role: role, name: name, secret: tag === "input" && type === "password" };
  }

  function push(rec) {
    try { top.__cua_human.push(rec); } catch (e) {}
  }

  function attach(doc) {
    if (!doc) return;
    /* Always walk into child frames, even when this document is already hooked:
       a navigation inside a frame replaces that frame's document, and the parent
       document's guard flag must not stop the new one from being hooked. */
    var already = false;
    try { already = !!doc.__cua_rec; doc.__cua_rec = true; } catch (e) { return; }
    if (already) { attachFrames(doc); return; }

    doc.addEventListener("click", function (e) {
      var d = describe(e.target);
      push({
        at: new Date().toISOString(),
        kind: "click",
        role: d.role,
        name: d.name,
        text: txt(e.target),
        frame_url: doc.location ? doc.location.href : "",
        x: e.clientX, y: e.clientY,
      });
    }, true);

    doc.addEventListener("change", function (e) {
      var d = describe(e.target);
      var v = null;
      try { v = d.secret ? "[REDACTED:secret-field]" : String(e.target.value ?? ""); } catch (err) {}
      push({
        at: new Date().toISOString(),
        kind: "input",
        role: d.role,
        name: d.name,
        value: v,
        frame_url: doc.location ? doc.location.href : "",
      });
    }, true);

    doc.addEventListener("submit", function (e) {
      push({
        at: new Date().toISOString(),
        kind: "submit",
        role: "form",
        name: at(e.target, "name") || at(e.target, "action") || "form",
        frame_url: doc.location ? doc.location.href : "",
      });
    }, true);

    attachFrames(doc);
  }

  function attachFrames(doc) {
    var fr = doc.querySelectorAll ? doc.querySelectorAll("iframe,frame") : [];
    for (var i = 0; i < fr.length; i++) {
      try { if (fr[i].contentDocument) attach(fr[i].contentDocument); } catch (e) {}
    }
  }

  attach(document);
  return (top.__cua_human || []).length;
})
