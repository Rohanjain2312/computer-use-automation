/*
 * Perception: build an accessibility-style tree from what is actually rendered.
 *
 * This deliberately does NOT rely on the page being well-formed. It recurses
 * through same-origin frames and framesets, computes a role from behaviour as
 * well as from tag names (a <td onclick=...> is a button to a human, so it is a
 * button here), derives an accessible name from the nearest thing a human would
 * read as the label (including the table cell to the left, which is how legacy
 * forms are actually labelled), and reports absolute viewport coordinates so
 * that clicks land where the screenshot shows them.
 *
 * Returns { url, title, text, frames, nodes } and leaves a ref -> element
 * registry on window.__cua for value reads and combobox writes.
 */
(function (opts) {
  var MAX_NODES = (opts && opts.maxNodes) || 400;
  var MAX_TEXT = (opts && opts.maxText) || 14000;
  var reg = [];
  var nodes = [];
  var frames = [];
  var textParts = [];

  function vis(el, win) {
    try {
      var cs = win.getComputedStyle(el);
      if (!cs) return false;
      if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
      var r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    } catch (e) {
      return false;
    }
  }

  function txt(el) {
    if (!el) return "";
    var t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    return t.length > 300 ? t.slice(0, 300) : t;
  }

  function attr(el, n) {
    try { return el.getAttribute(n) || ""; } catch (e) { return ""; }
  }

  function behavesClickable(el, win) {
    if (attr(el, "onclick")) return true;
    try {
      if (win.getComputedStyle(el).cursor === "pointer" && txt(el)) return true;
    } catch (e) {}
    return false;
  }

  function roleOf(el, win) {
    var explicit = attr(el, "role");
    if (explicit) return { role: explicit.toLowerCase(), src: "aria" };
    var tag = el.tagName.toLowerCase();
    if (tag === "input") {
      var t = (attr(el, "type") || "text").toLowerCase();
      if (t === "submit" || t === "button" || t === "reset" || t === "image")
        return { role: "button", src: "tag" };
      if (t === "checkbox") return { role: "checkbox", src: "tag" };
      if (t === "radio") return { role: "radio", src: "tag" };
      if (t === "hidden") return null;
      return { role: "textbox", src: "tag" };
    }
    if (tag === "button") return { role: "button", src: "tag" };
    if (tag === "textarea") return { role: "textbox", src: "tag" };
    if (tag === "select") return { role: "combobox", src: "tag" };
    if (tag === "a" && attr(el, "href")) return { role: "link", src: "tag" };
    if (/^h[1-6]$/.test(tag)) return { role: "heading", src: "tag" };
    if (tag === "td" || tag === "th") {
      if (behavesClickable(el, win)) return { role: "button", src: "behavior" };
      return { role: "cell", src: "tag" };
    }
    if ((tag === "div" || tag === "span" || tag === "li") && behavesClickable(el, win))
      return { role: "button", src: "behavior" };
    return null;
  }

  /* Text a human would read as this element's label: the cell to the left, or above. */
  function clean(t) { return (t || "").replace(/[:*]\s*$/, ""); }

  function neighbourLabels(el) {
    /* Returns { left, above, prev, list }. The distinction matters: in a
       key/value panel the label is the cell to the LEFT, while the cell ABOVE is
       just another value. Callers that know the table shape pick the right one. */
    var left = "", above = "", prev = "";
    var cell = el.closest ? el.closest("td,th") : null;
    if (cell) {
      var p = cell.previousElementSibling;
      while (p) {
        var pt = txt(p);
        if (pt) { left = clean(pt); break; }
        p = p.previousElementSibling;
      }
      var row = cell.closest("tr");
      if (row) {
        var idx = Array.prototype.indexOf.call(row.children, cell);
        var prevRow = row.previousElementSibling;
        if (prevRow && prevRow.children[idx]) above = clean(txt(prevRow.children[idx]));
      }
    }
    var pn = el.previousElementSibling;
    if (pn && txt(pn) && txt(pn).length < 60) prev = clean(txt(pn));
    var list = [];
    [left, above, prev].forEach(function (c) { if (c && list.indexOf(c) < 0) list.push(c); });
    return { left: left, above: above, prev: prev, list: list };
  }

  /* Accessible name, in the order a human resolves it. */
  function nameOf(el, doc, win, role) {
    var v;
    var nb = neighbourLabels(el);
    var cands = nb.list;
    v = attr(el, "aria-label");
    if (v) return { name: v.trim(), src: "aria-label", cands: cands, nb: nb };

    var lb = attr(el, "aria-labelledby");
    if (lb) {
      var parts = [];
      lb.split(/\s+/).forEach(function (id) {
        var t = doc.getElementById(id);
        if (t) parts.push(txt(t));
      });
      if (parts.join(" ").trim()) return { name: parts.join(" ").trim(), src: "aria-labelledby", cands: cands, nb: nb };
    }

    if (el.id) {
      var lab = doc.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab && txt(lab)) return { name: txt(lab), src: "label-for", cands: cands, nb: nb };
    }

    var wrap = el.closest ? el.closest("label") : null;
    if (wrap && txt(wrap)) return { name: txt(wrap), src: "label-wrap", cands: cands, nb: nb };

    if (role === "button" || role === "link" || role === "heading" || role === "cell") {
      var tag = el.tagName.toLowerCase();
      if (tag === "input") {
        v = attr(el, "value") || attr(el, "alt");
        if (v) return { name: v.trim(), src: "value-attr", cands: cands, nb: nb };
      }
      var it = txt(el);
      if (it) return { name: it, src: "inner-text", cands: cands, nb: nb };
    }

    v = attr(el, "placeholder");
    if (v) return { name: v.trim(), src: "placeholder", cands: cands, nb: nb };
    v = attr(el, "title");
    if (v) return { name: v.trim(), src: "title", cands: cands, nb: nb };
    if (nb.left) return { name: nb.left, src: "label-proximity", cands: cands, nb: nb };
    if (cands.length) return { name: cands[0], src: "label-proximity", cands: cands, nb: nb };
    v = attr(el, "name");
    if (v) return { name: v, src: "name-attr", cands: cands, nb: nb };
    return { name: "", src: "none", cands: cands, nb: nb };
  }

  function headerRow(tableEl) {
    var rows = tableEl.querySelectorAll("tr");
    for (var i = 0; i < rows.length && i < 3; i++) {
      var cells = rows[i].children;
      var hs = [];
      var anyTh = false;
      for (var j = 0; j < cells.length; j++) {
        if (cells[j].tagName.toLowerCase() === "th") anyTh = true;
        hs.push(txt(cells[j]));
      }
      var looksHeader = anyTh || (cells.length > 1 && hs.every(function (h) { return h && h.length < 40; }));
      if (!looksHeader) continue;
      /* A key/value panel ("Member Number | 100244 | Status | ACTIVE") also passes
         the shape test, so only trust inferred headers when they read like column
         names: at least two of them, all distinct, none a bare value. */
      var distinct = hs.filter(function (h, k) { return hs.indexOf(h) === k; }).length === hs.length;
      var valueLike = hs.some(function (h) { return /^[$]?[\d.,%-]+$/.test(h); });
      /* ...and only when it is the table's FIRST row. A "header" found further
         down is a data row that happens to look tidy — a two-column form panel
         whose second row reads "Inquiry Type | Account Summary" would otherwise
         be treated as a column table. */
      var confident = anyTh || (i === 0 && hs.length > 1 && distinct && !valueLike);
      return { headers: hs, rowIndex: i, confident: confident };
    }
    return null;
  }

  function tableCtx(el) {
    var cell = el.closest ? el.closest("td,th") : null;
    if (!cell) return null;
    var tableEl = cell.closest("table");
    if (!tableEl) return null;
    var row = cell.closest("tr");
    var allRows = Array.prototype.slice.call(tableEl.querySelectorAll("tr")).filter(function (r) {
      return r.closest("table") === tableEl;
    });
    var rowIndex = allRows.indexOf(row);
    var colIndex = Array.prototype.indexOf.call(row.children, cell);
    var hr = headerRow(tableEl);
    var headers = hr ? hr.headers : [];
    var colHeader = headers[colIndex] || "";
    var rowLabel = row.children[0] ? txt(row.children[0]) : "";
    return {
      headers: headers,
      row_index: rowIndex,
      col_index: colIndex,
      row_label: rowLabel,
      col_header: colHeader,
      header_confident: !!(hr && hr.confident),
    };
  }

  function cssHint(el) {
    var tag = el.tagName.toLowerCase();
    var bits = [tag];
    if (el.id) bits.push("#" + el.id);
    var nm = attr(el, "name");
    if (nm) bits.push('[name="' + nm + '"]');
    return bits.join("");
  }

  function pushNode(el, win, doc, offX, offY, framePath, frameName) {
    if (nodes.length >= MAX_NODES) return;
    var r = roleOf(el, win);
    if (!r) return;
    if (!vis(el, win)) return;
    var isCell = r.role === "cell";
    var cellText = isCell ? txt(el) : "";
    if (isCell && !cellText) return;

    var nm = nameOf(el, doc, win, r.role);
    if (!isCell && !nm.name && r.role !== "textbox" && r.role !== "combobox") return;

    var bb = el.getBoundingClientRect();
    var tag = el.tagName.toLowerCase();
    var isSecret = tag === "input" && (attr(el, "type") || "").toLowerCase() === "password";
    var val = "";
    try {
      if (tag === "input" || tag === "textarea") val = isSecret ? "" : String(el.value || "");
      else if (tag === "select") val = String(el.value || "");
    } catch (e) {}

    var ref = "n" + reg.length;
    reg.push(el);
    nodes.push({
      ref: ref,
      role: r.role,
      role_source: r.src,
      name: nm.name,
      name_source: nm.src,
      label_candidates: nm.cands.slice(0, 3),
      label_left: (nm.nb && nm.nb.left) || "",
      label_above: (nm.nb && nm.nb.above) || "",
      value: val,
      text: isCell ? cellText : txt(el),
      enabled: !el.disabled,
      secret: isSecret,
      rect: {
        x: Math.round((bb.left + offX) * 10) / 10,
        y: Math.round((bb.top + offY) * 10) / 10,
        width: Math.round(bb.width * 10) / 10,
        height: Math.round(bb.height * 10) / 10,
      },
      in_viewport:
        bb.top + offY >= -2 &&
        bb.left + offX >= -2 &&
        bb.top + offY < (window.innerHeight || 900) &&
        bb.left + offX < (window.innerWidth || 1280),
      frame_path: framePath,
      frame_name: frameName,
      table: tableCtx(el),
      dom_hint: { tag: tag, css: cssHint(el), name_attr: attr(el, "name"), id: el.id || "" },
    });
  }

  function walk(doc, win, offX, offY, framePath, frameName, depth) {
    if (depth > 4) return;
    try {
      var body = doc.body || doc.documentElement;
      if (body) {
        var t = (body.innerText || "").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
        if (t) textParts.push(t);
      }
      var all = doc.querySelectorAll("*");
      for (var i = 0; i < all.length; i++) pushNode(all[i], win, doc, offX, offY, framePath, frameName);

      var fr = doc.querySelectorAll("iframe,frame");
      for (var k = 0; k < fr.length; k++) {
        var f = fr[k];
        var fb = f.getBoundingClientRect();
        var childOffX = offX + fb.left;
        var childOffY = offY + fb.top;
        var fname = attr(f, "name") || attr(f, "id") || "frame" + k;
        var fpath = framePath ? framePath + "/" + fname : fname;
        var cdoc = null;
        try { cdoc = f.contentDocument; } catch (e) { cdoc = null; }
        var furl = "";
        try { furl = cdoc ? cdoc.location.href : ""; } catch (e) { furl = ""; }
        frames.push({
          name: fname,
          path: fpath,
          src: attr(f, "src"),
          url: furl,
          accessible: !!cdoc,
          rect: { x: childOffX, y: childOffY, width: fb.width, height: fb.height },
        });
        if (cdoc) walk(cdoc, f.contentWindow || win, childOffX, childOffY, fpath, fname, depth + 1);
      }
    } catch (e) {
      textParts.push("[perception error: " + e.message + "]");
    }
  }

  walk(document, window, 0, 0, "", "", 0);
  window.__cua = { nodes: reg };

  var text = textParts.join("\n---\n");
  if (text.length > MAX_TEXT) text = text.slice(0, MAX_TEXT) + "\n[truncated]";

  return {
    url: location.href,
    title: document.title,
    text: text,
    frames: frames,
    nodes: nodes,
    truncated_nodes: nodes.length >= MAX_NODES,
  };
})
