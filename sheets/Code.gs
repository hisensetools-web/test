/**
 * Shopify Early-Scaling Tracker -> Google Sheets endpoint.
 *
 * Paste this whole file into a Google Apps Script project that is BOUND to the
 * spreadsheet you want to fill (Extensions > Apps Script from inside the sheet),
 * then Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone.
 * Put the resulting /exec URL in .env as SHEETS_WEBHOOK_URL.
 *
 * Protocol (one POST per chunk, JSON body):
 *   { "tab": "Signals" | "Families" | "Categories" | "Stores" | "Store Age" | "Products" | "Alerts",
 *     "mode": "replace" | "append",
 *     "chunk": 1, "chunks": 3,          // 1-based; replace clears the tab on chunk 1
 *     "rows": [[...], [...]] }           // values in header order
 * Reply: { "ok": true, "tab": "...", "received": n, "written": m, "skipped": k }
 *    or  { "ok": false, "error": "..." }
 *
 * Tabs are created with a bold, frozen header row and auto-sized columns the first
 * time they are written. "Alerts" is de-duplicated on its key columns so re-running a
 * sync never creates duplicate rows; every other tab is rewritten in full each sync.
 */

var TABS = {
  Signals: {
    headers: ["store", "product family", "handle", "channel tag", "days_since_published", "published_at",
              "price", "sold_out", "collection_rank", "collection_rank_delta_7d",
              "variants_of_family_published_7d", "ads_pointing_here", "engagement_per_day",
              "days_running_max", "concept_status", "eu_reach_slope_7d", "comment_delta_1d",
              "signal_source", "inventory_tracked", "stock_level", "units_sold_1d", "units_per_day_7d",
              "units_per_day_wow", "store_created_est", "store_age_days"],
    keyCols: null, textCols: [0, 1, 2, 3, 5, 7, 14, 15, 16, 23], position: 1
  },
  Families: {
    headers: ["store", "family", "title", "handles", "newest published_at", "oldest published_at",
              "published 7d", "published 14d", "published 30d", "best collection rank", "handle list"],
    keyCols: null, textCols: [0, 1, 2, 4, 5, 10], position: 2
  },
  Categories: {
    headers: ["category", "stores", "families", "newest published_at", "families published 7d",
              "store list", "example families"],
    keyCols: null, textCols: [0, 3, 5, 6], position: 3
  },
  Stores: {
    headers: ["store", "meta page", "last status", "products", "sold-out variants",
              "new products 7d", "updated products 7d", "sold-out delta", "price changes",
              "change score", "last snapshot date", "shop_id", "myshopify", "store_created_est", "store_age_days"],
    keyCols: null,                 // fully overwritten each sync
    textCols: [0, 1, 2, 10, 12, 13],   // keep dates / domains as text, not auto-parsed
    position: 4
  },
  "Store Age": {
    headers: ["store", "shop_id", "myshopify", "store_created_est", "store_age_days", "method",
              "lower calibration", "upper calibration", "products", "first snapshot", "id source", "error"],
    keyCols: null,                 // newest store first; fully overwritten each sync
    textCols: [0, 2, 3, 5, 6, 7, 9, 10, 11],
    position: 5
  },
  Products: {
    headers: ["date", "store", "handle", "title", "published_at", "updated_at", "price",
              "available variants", "total variants", "collection position"],
    keyCols: null,                 // the full latest catalogue of every store, rewritten each sync
    textCols: [0, 1, 2, 3, 4, 5],  // (history stays in data/tracker.db: `python tracker.py product <handle>`)
    moveToEnd: true,               // raw data lives at the end of the tab bar
    hideCols: [7, 8]               // available / total variants (still there, just hidden)
  },
  Alerts: {
    headers: ["date", "store", "handle", "rule", "detail", "created_at"],
    keyCols: [0, 1, 2, 3, 4],      // date + store + handle + rule + detail (several alerts can share a rule)
    textCols: [0, 1, 2, 4, 5],
    position: 6
  }
};

/** Health check: open the /exec URL in a browser and you should see "ok".
 *  ?tabs=1            -> JSON {tab: rows} for every tab the script knows (rows exclude the header)
 *  ?tab=Products      -> JSON {tab, rows, maxRows}
 *  ?tab=Products&group=1 -> also {byValue: {value in column 1 (0-based): count}}, e.g. rows per store */
function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.tabs) {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var out = {};
    for (var name in TABS) {
      var sh = ss.getSheetByName(name);
      out[name] = sh ? Math.max(0, sh.getLastRow() - 1) : null;
    }
    return json_({ ok: true, tabs: out });
  }
  if (p.tab) {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(p.tab);
    if (!sheet) return json_({ ok: true, tab: p.tab, rows: null, maxRows: null });
    var last = sheet.getLastRow();
    var res = { ok: true, tab: p.tab, rows: Math.max(0, last - 1), maxRows: sheet.getMaxRows() };
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
    var spec = TABS[body.tab];
    if (!spec) throw new Error("unknown tab: " + body.tab + " (expected one of " + Object.keys(TABS).join(", ") + ")");
    var rows = Array.isArray(body.rows) ? body.rows : [];
    var sheet = getOrCreateSheet_(body.tab, spec);
    var result = (body.mode === "replace")
      ? replaceRows_(sheet, spec, rows, body.chunk || 1)
      : appendRows_(sheet, spec, rows);
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

function getOrCreateSheet_(name, spec) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(name);
  var isNew = false;
  if (!sheet) {
    sheet = ss.insertSheet(name);
    isNew = true;
  }
  if (isNew || sheet.getLastRow() === 0) {
    sheet.getRange(1, 1, 1, spec.headers.length).setValues([spec.headers]).setFontWeight("bold");
    sheet.setFrozenRows(1);
    for (var i = 0; i < spec.textCols.length; i++) {
      sheet.getRange(1, spec.textCols[i] + 1, sheet.getMaxRows(), 1).setNumberFormat("@");
    }
    sheet.autoResizeColumns(1, spec.headers.length);
    if (spec.position) moveSheet_(ss, sheet, spec.position);
  }
  if (spec.moveToEnd) moveSheet_(ss, sheet, ss.getNumSheets());
  if (spec.hideCols) {
    for (var h = 0; h < spec.hideCols.length; h++) sheet.hideColumns(spec.hideCols[h] + 1);
  }
  return sheet;
}

function moveSheet_(ss, sheet, position) {
  var n = ss.getNumSheets();
  var target = Math.max(1, Math.min(position, n));
  if (sheet.getIndex && sheet.getIndex() === target) return;
  ss.setActiveSheet(sheet);
  ss.moveActiveSheet(target);
}

/** Stores: chunk 1 wipes everything below the header, later chunks append. */
function replaceRows_(sheet, spec, rows, chunk) {
  if (chunk === 1) {
    var last = sheet.getLastRow();
    if (last > 1) sheet.getRange(2, 1, last - 1, sheet.getMaxColumns()).clearContent();
  }
  writeRows_(sheet, spec, rows);
  return { received: rows.length, written: rows.length, skipped: 0 };
}

/** Products / Alerts: append rows whose key is not already present. */
function appendRows_(sheet, spec, rows) {
  var existing = {};
  var last = sheet.getLastRow();
  var keyWidth = Math.max.apply(null, spec.keyCols) + 1;
  if (last > 1) {
    var vals = sheet.getRange(2, 1, last - 1, keyWidth).getValues();
    for (var i = 0; i < vals.length; i++) existing[keyOf_(vals[i], spec.keyCols)] = true;
  }
  var fresh = [];
  for (var j = 0; j < rows.length; j++) {
    var key = keyOf_(rows[j], spec.keyCols);
    if (existing[key]) continue;
    existing[key] = true;
    fresh.push(rows[j]);
  }
  writeRows_(sheet, spec, fresh);
  return { received: rows.length, written: fresh.length, skipped: rows.length - fresh.length };
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
  // invalid"); new sheets have 1000 rows, so a full catalogue used to fail part-way. Grow first.
  var need = start + padded.length - 1 - sheet.getMaxRows();
  if (need > 0) sheet.insertRowsAfter(sheet.getMaxRows(), need);
  if (sheet.getMaxColumns() < width) sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
  var range = sheet.getRange(start, 1, padded.length, width);
  // Force text format on key/date columns BEFORE writing so "2026-09-06" and ISO
  // timestamps stay strings (otherwise Sheets parses them into dates and the
  // de-dup keys stop matching).
  for (var i = 0; i < spec.textCols.length; i++) {
    sheet.getRange(start, spec.textCols[i] + 1, padded.length, 1).setNumberFormat("@");
  }
  range.setValues(padded);
}

function keyOf_(row, keyCols) {
  var parts = [];
  for (var i = 0; i < keyCols.length; i++) parts.push(cellText_(row[keyCols[i]]));
  return parts.join("");
}

/** Normalise a cell for key comparison; dates that slipped through become yyyy-MM-dd. */
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
