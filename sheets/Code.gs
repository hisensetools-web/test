/**
 * Shopify Early-Scaling Tracker -> Google Sheets endpoint.
 *
 * Paste this whole file into a Google Apps Script project that is BOUND to the
 * spreadsheet you want to fill (Extensions > Apps Script from inside the sheet),
 * then Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone.
 * Put the resulting /exec URL in .env as SHEETS_WEBHOOK_URL.
 *
 * Protocol (one POST per chunk, JSON body):
 *   { "tab": "Stores" | "Products" | "Alerts",
 *     "mode": "replace" | "append",
 *     "chunk": 1, "chunks": 3,          // 1-based; replace clears the tab on chunk 1
 *     "rows": [[...], [...]] }           // values in header order
 * Reply: { "ok": true, "tab": "...", "received": n, "written": m, "skipped": k }
 *    or  { "ok": false, "error": "..." }
 *
 * Tabs are created with a bold, frozen header row and auto-sized columns the first
 * time they are written. "Products" and "Alerts" are de-duplicated on their key
 * columns so re-running a sync never creates duplicate rows.
 */

var TABS = {
  Stores: {
    headers: ["store", "meta page", "last status", "products", "sold-out variants",
              "new products 7d", "updated products 7d", "sold-out delta", "price changes",
              "change score", "last snapshot date"],
    keyCols: null,                 // fully overwritten each sync
    textCols: [0, 1, 2, 10]        // keep dates / domains as text, not auto-parsed
  },
  Products: {
    headers: ["date", "store", "handle", "title", "published_at", "updated_at", "price",
              "available variants", "total variants", "collection position"],
    keyCols: [0, 1, 2],            // date + store + handle
    textCols: [0, 1, 2, 3, 4, 5]
  },
  Alerts: {
    headers: ["date", "store", "handle", "rule", "detail", "created_at"],
    keyCols: [0, 1, 2, 3],         // date + store + handle + rule
    textCols: [0, 1, 2, 4, 5]
  }
};

/** Health check: open the /exec URL in a browser and you should see "ok". */
function doGet(e) {
  return ContentService.createTextOutput("ok");
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    if (!e || !e.postData || !e.postData.contents) throw new Error("empty POST body");
    var body = JSON.parse(e.postData.contents);
    var spec = TABS[body.tab];
    if (!spec) throw new Error("unknown tab: " + body.tab + " (expected Stores, Products or Alerts)");
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
  }
  return sheet;
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
