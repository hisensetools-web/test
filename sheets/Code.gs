/**
 * Shopify Early-Scaling Tracker -> Google Sheets endpoint.
 *
 * Paste this whole file into a Google Apps Script project that is BOUND to the
 * spreadsheet you want to fill (Extensions > Apps Script from inside the sheet),
 * then Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone.
 * Put the resulting /exec URL in .env as SHEETS_WEBHOOK_URL.
 *
 * Protocol (one POST per chunk, JSON body):
 *   { "tab": "Winners" | "Stores",
 *     "mode": "replace",
 *     "chunk": 1, "chunks": 3,          // 1-based; replace clears the tab on chunk 1
 *     "rows": [[...], [...]] }           // values in header order
 *   { "mode": "diag", "text": "..." }    // the diagnostics report -> Google Doc "EarlyScale Diag"
 * Reply: { "ok": true, "tab": "...", "received": n, "written": m, "skipped": k }
 *    or  { "ok": false, "error": "..." }
 *
 * Tabs are created with a bold, frozen header row and auto-sized columns the first
 * time they are written; every tab is rewritten in full each sync.
 */

var TABS = {
  Winners: {
    headers: ["store", "landing_url", "delivering", "delivering_7d_ago", "delivering_wow", "proven_days", "pages", "pages_new_7d", "top_page", "family_age_days", "store_age_days", "ads_as_of"],
    keyCols: null, textCols: [0, 1, 4, 8, 11], position: 1
  },
  Stores: {
    headers: ["store", "shop_id", "store_age_days", "products", "ads_as_of", "last error"],
    keyCols: null, textCols: [0, 1, 4, 5], position: 2   // shop_id as text: a 12-digit id must not become 1.2E+11
  }
};

/** Health check: open the /exec URL in a browser and you should see "ok".
 *  ?tabs=1            -> JSON {tab: rows} for every tab the script knows (rows exclude the header) + the headers it writes
 *  ?tab=Winners       -> JSON {tab, rows, maxRows}
 *  ?tab=Winners&group=0 -> also {byValue: {value in column 0 (0-based): count}}, e.g. rows per store
 *  ?tab=Winners&rows=1 -> also {rows: [[...]]}; add &cols=store,delivering to get only those columns (by header name) */
function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.tabs) {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var out = {}, heads = {};
    for (var name in TABS) {
      var sh = ss.getSheetByName(name);
      out[name] = sh ? Math.max(0, sh.getLastRow() - 1) : null;
      heads[name] = TABS[name].headers;          // what this deployment writes; the tracker refuses to sync onto a stale set
    }
    return json_({ ok: true, tabs: out, headers: heads });
  }
  if (p.tab) {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(p.tab);
    if (!sheet) return json_({ ok: true, tab: p.tab, rows: null, maxRows: null });
    var last = sheet.getLastRow();
    var res = { ok: true, tab: p.tab, rows: Math.max(0, last - 1), maxRows: sheet.getMaxRows() };
    if (p.rows !== undefined && last > 1) {
      var n = Math.min(last - 1, 2000);
      var w = Math.min(sheet.getLastColumn(), 40);
      var vals2 = sheet.getRange(2, 1, n, w).getValues();
      var fmt = function (r) { return r.map(function (v) { return v instanceof Date ? Utilities.formatDate(v, "UTC", "yyyy-MM-dd") : v; }); };
      if (p.cols) {
        var head = sheet.getRange(1, 1, 1, w).getValues()[0].map(String);
        var idx = String(p.cols).split(",").map(function (c) { return head.indexOf(c); });
        res.cols = String(p.cols).split(",");
        res.rows = vals2.map(function (r) { return fmt(idx.map(function (i) { return i >= 0 ? r[i] : ""; })); });
      } else {
        res.rows = vals2.map(fmt);
      }
    }
    if (p.group !== undefined && last > 1) {
      var col = parseInt(p.group, 10) + 1;
      var vals = sheet.getRange(2, col, last - 1, 1).getValues();
      var by = {};
      for (var i = 0; i < vals.length; i++) { var k = cellText_(vals[i][0]); by[k] = (by[k] || 0) + 1; }
      res.byValue = by;
    }
    return json_(res);
  }
  return ContentService.createTextOutput("ok");
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error("empty POST body");
    var body = JSON.parse(e.postData.contents);
    if (body.mode === "diag") return json_(writeDiag_(String(body.text || "")));
    var spec = TABS[body.tab];
    if (!spec) throw new Error("unknown tab: " + body.tab + " (expected one of " + Object.keys(TABS).join(", ") + ")");
    var rows = Array.isArray(body.rows) ? body.rows : [];
    var sheet = getOrCreateSheet_(body.tab, spec);
    var result = replaceRows_(sheet, spec, rows, body.chunk || 1);
    result.ok = true;
    result.tab = body.tab;
    result.chunk = body.chunk || 1;
    result.chunks = body.chunks || 1;
    return json_(result);
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message ? err.message : err) });
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------- helpers

/** `python tracker.py diag`: the diagnostics report goes into a Google Doc named "EarlyScale Diag" in My Drive
 *  (created on first use, its id remembered in the script properties, replaced on every push) so it can be read
 *  without pasting anything. Needs the Google Docs permission ONCE: in the Apps Script editor pick the function
 *  `authorizeDiag` in the toolbar dropdown, click Run, accept the prompt, then Deploy > Manage deployments >
 *  Edit > New version. (A web app only holds the permissions granted when its owner ran / authorized it.) */
function writeDiag_(text) {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty("diagDocId");
  var doc = null;
  if (id) {
    try { doc = DocumentApp.openById(id); } catch (err) { doc = null; }   // deleted by hand: create a new one
  }
  if (!doc) {
    doc = DocumentApp.create("EarlyScale Diag");
    props.setProperty("diagDocId", doc.getId());
  }
  var body = doc.getBody();
  body.clear();
  body.setText(text);
  doc.saveAndClose();
  return { ok: true, doc: doc.getUrl(), chars: text.length };
}

/** Run this once from the editor (dropdown > authorizeDiag > Run) to grant the Docs permission the web app needs. */
function authorizeDiag() {
  var r = writeDiag_("EarlyScale Diag: authorized on " + new Date().toISOString() + ". The tracker replaces this text on every push.");
  Logger.log("ok: " + r.doc);
  return r.doc;
}

function getOrCreateSheet_(name, spec) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(name);
  var isNew = false;
  if (!sheet) {
    sheet = ss.insertSheet(name);
    isNew = true;
  }
  ensureHeader_(sheet, spec);
  if (isNew && spec.position) moveSheet_(ss, sheet, spec.position);
  return sheet;
}

function moveSheet_(ss, sheet, position) {
  var n = ss.getNumSheets();
  var target = Math.max(1, Math.min(position, n));
  if (sheet.getIndex && sheet.getIndex() === target) return;
  ss.setActiveSheet(sheet);
  ss.moveActiveSheet(target);
}

/** chunk 1 wipes everything below the header, later chunks append. */
function replaceRows_(sheet, spec, rows, chunk) {
  if (chunk === 1) {
    var last = sheet.getLastRow();
    if (last > 1) sheet.getRange(2, 1, last - 1, sheet.getMaxColumns()).clearContent();
  }
  writeRows_(sheet, spec, rows);
  return { received: rows.length, written: rows.length, skipped: 0 };
}

/** Header row = the spec's headers, always: tabs gain columns over time and a stale header is worse
 *  than none. Grows the grid first (a new sheet has 26 columns). */
function ensureHeader_(sheet, spec) {
  var width = spec.headers.length;
  if (sheet.getMaxColumns() < width) sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  var cur = sheet.getLastRow() >= 1 ? sheet.getRange(1, 1, 1, width).getValues()[0] : [];
  var same = cur.length === width;
  for (var i = 0; same && i < width; i++) if (String(cur[i]) !== String(spec.headers[i])) same = false;
  if (same) return;
  sheet.getRange(1, 1, 1, width).setValues([spec.headers]).setFontWeight("bold");
  sheet.setFrozenRows(1);
  for (var j = 0; j < spec.textCols.length; j++) {
    sheet.getRange(1, spec.textCols[j] + 1, sheet.getMaxRows(), 1).setNumberFormat("@");
  }
  sheet.autoResizeColumns(1, width);
}

function writeRows_(sheet, spec, rows) {
  if (!rows.length) return;
  var width = spec.headers.length;
  var padded = rows.map(function (r) {
    var out = [];
    for (var c = 0; c < width; c++) {
      var v = (r && c < r.length) ? r[c] : "";
      out.push(v === null || v === undefined ? "" : v);
    }
    return out;
  });
  var start = sheet.getLastRow() + 1;
  // A range past the grid throws in Apps Script ("coordinates or dimensions of the range are
  // invalid"); new sheets have 1000 rows. Grow first.
  var need = start + padded.length - 1 - sheet.getMaxRows();
  if (need > 0) sheet.insertRowsAfter(sheet.getMaxRows(), need);
  if (sheet.getMaxColumns() < width) sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  var range = sheet.getRange(start, 1, padded.length, width);
  // Force text format on key/date columns BEFORE writing so "2026-09-06" stays a string.
  for (var i = 0; i < spec.textCols.length; i++) {
    sheet.getRange(start, spec.textCols[i] + 1, padded.length, 1).setNumberFormat("@");
  }
  range.setValues(padded);
}

/** Normalise a cell for grouping; dates that slipped through become yyyy-MM-dd. */
function cellText_(v) {
  if (v === null || v === undefined) return "";
  if (Object.prototype.toString.call(v) === "[object Date]") {
    var tz = SpreadsheetApp.getActiveSpreadsheet().getSpreadsheetTimeZone();
    return Utilities.formatDate(v, tz, "yyyy-MM-dd");
  }
  return String(v).trim();
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
