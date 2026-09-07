// Runs the REAL sheets/Code.gs inside a tiny in-memory imitation of the Apps Script
// runtime (SpreadsheetApp / ContentService / LockService / Utilities) behind an HTTP
// server that behaves like a deployed web app: POST -> 302 -> GET returns the JSON.
//
//   node tests/fake_gas.js --port 8090 [--fail-first]
//   GET /exec            -> doGet()
//   POST /exec           -> doPost(), 302 to /r/<id>
//   GET /r/<id>          -> the stored doPost response
//   GET /__dump          -> {sheetName: {frozenRows, rows:[[...]]}} for assertions
//   GET /__login         -> an HTML page (simulates a deployment that is not "Anyone")
"use strict";
const fs = require("fs"), http = require("http"), path = require("path"), vm = require("vm");

const args = process.argv.slice(2);
const port = Number(args[args.indexOf("--port") + 1] || 8090);
let failFirst = args.includes("--fail-first");

// ------------------------------------------------ fake Sheets model
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
class Sheet {
  constructor(name) { this.name = name; this.cells = []; this.formats = {}; this.frozenRows = 0; this.maxRows = 1000; this.autoResized = 0; this.hidden = new Set(); }
  hideColumns(c, n = 1) { for (let i = 0; i < n; i++) this.hidden.add(c + i); }
  getIndex() { return order.indexOf(this) + 1; }
  _fmt(r, c) { return this.formats[r + ":" + c] || this.formats["*:" + c] || ""; }
  getName() { return this.name; }
  getLastRow() { let last = 0; this.cells.forEach((row, i) => { if (row && row.some(v => v !== "" && v !== null && v !== undefined)) last = i + 1; }); return last; }
  getMaxRows() { return Math.max(this.maxRows, this.cells.length); }
  getMaxColumns() { return this.maxCols || 26; }
  getLastColumn() { let w = 0; this.cells.forEach(r => { if (r) w = Math.max(w, r.length); }); return w; }
  setFrozenRows(n) { this.frozenRows = n; }
  autoResizeColumns(c, n) { this.autoResized = n; }
  getRange(r, c, nr = 1, nc = 1) {
    // Real Apps Script throws for a range outside the grid; keep the fake honest so the
    // "grow the sheet first" logic in Code.gs is actually exercised.
    if (r < 1 || c < 1 || nr < 1 || nc < 1 || r + nr - 1 > this.getMaxRows() || c + nc - 1 > this.getMaxColumns())
      throw new Error("The coordinates or dimensions of the range are invalid.");
    return new Range(this, r, c, nr, nc);
  }
  insertRowsAfter(after, n) { this.maxRows = Math.max(this.maxRows, after) + n; return this; }
  insertColumnsAfter(after, n) { this.maxCols = Math.max(this.maxCols || 26, after) + n; return this; }
}
class Range {
  constructor(s, r, c, nr, nc) { Object.assign(this, { s, r, c, nr, nc }); }
  setNumberFormat(f) {
    for (let i = 0; i < this.nr; i++) for (let j = 0; j < this.nc; j++) {
      if (this.nr >= this.s.getMaxRows()) this.s.formats["*:" + (this.c + j)] = f;
      this.s.formats[(this.r + i) + ":" + (this.c + j)] = f;
    }
    return this;
  }
  setFontWeight() { return this; }
  setValues(vals) {
    if (vals.length !== this.nr || vals.some(v => v.length !== this.nc)) throw new Error("setValues shape mismatch");
    for (let i = 0; i < this.nr; i++) {
      const row = this.r - 1 + i;
      if (!this.s.cells[row]) this.s.cells[row] = [];
      for (let j = 0; j < this.nc; j++) {
        let v = vals[i][j];
        // Imitate Sheets auto-parsing "2026-09-06" into a Date unless the cell is text-formatted.
        if (typeof v === "string" && DATE_RE.test(v) && this.s._fmt(this.r + i, this.c + j) !== "@") v = new Date(v + "T00:00:00Z");
        this.s.cells[row][this.c - 1 + j] = v;
      }
    }
    return this;
  }
  getValues() {
    const out = [];
    for (let i = 0; i < this.nr; i++) { const row = this.s.cells[this.r - 1 + i] || []; const o = []; for (let j = 0; j < this.nc; j++) { const v = row[this.c - 1 + j]; o.push(v === undefined ? "" : v); } out.push(o); }
    return out;
  }
  clearContent() { for (let i = 0; i < this.nr; i++) { const row = this.s.cells[this.r - 1 + i]; if (row) for (let j = 0; j < this.nc; j++) row[this.c - 1 + j] = ""; } return this; }
}
const sheets = {}; const order = []; let active = null;
const spreadsheet = {
  getSheetByName: n => sheets[n] || null,
  insertSheet: n => { const s = new Sheet(n); sheets[n] = s; order.push(s); return s; },
  getSpreadsheetTimeZone: () => "Etc/UTC",
  getNumSheets: () => order.length,
  getSheets: () => order.slice(),
  setActiveSheet: s => { active = s; return s; },
  moveActiveSheet: pos => { const i = order.indexOf(active); if (i < 0) throw new Error("no active sheet"); order.splice(i, 1); order.splice(pos - 1, 0, active); },
};
const sandbox = {
  SpreadsheetApp: { getActiveSpreadsheet: () => spreadsheet },
  ContentService: {
    MimeType: { JSON: "application/json", TEXT: "text/plain" },
    createTextOutput: t => { const o = { content: t, mime: "text/plain" }; o.setMimeType = m => { o.mime = m; return o; }; o.getContent = () => o.content; return o; },
  },
  LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() {} }) },
  Utilities: { formatDate: (d, tz, fmt) => d.toISOString().slice(0, 10) },
  console,
};
vm.createContext(sandbox);
let code = fs.readFileSync(path.join(__dirname, "..", "sheets", "Code.gs"), "utf8");
// print the stack of any Apps Script error to this process's stderr (the script itself only reports the message)
code = code.replace("return json_({ ok: false, error: String(err && err.message ? err.message : err) });",
                    "console.error('[Code.gs] ' + (err && err.stack ? err.stack : err)); return json_({ ok: false, error: String(err && err.message ? err.message : err) });");
vm.runInContext(code, sandbox, { filename: "Code.gs" });

// ------------------------------------------------ web-app-like HTTP front
const responses = {}; let seq = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (req.method === "GET" && url.pathname === "/exec") {
    const parameter = {}; for (const [k, v] of url.searchParams) parameter[k] = v;
    const o = sandbox.doGet({ parameter });
    res.writeHead(200, { "Content-Type": o.mime }); return res.end(o.content);
  }
  if (req.method === "GET" && url.pathname === "/__dump") {
    const out = { __order: order.map(s => s.name) }; for (const [n, s] of Object.entries(sheets)) out[n] = { frozenRows: s.frozenRows, autoResized: s.autoResized, hidden: [...s.hidden].sort((a, b) => a - b), rows: s.cells.slice(0, s.getLastRow()).map(r => r.map(v => v instanceof Date ? "DATE:" + v.toISOString().slice(0, 10) : v)) };
    res.writeHead(200, { "Content-Type": "application/json" }); return res.end(JSON.stringify(out));
  }
  if (url.pathname === "/__login") { res.writeHead(200, { "Content-Type": "text/html" }); return res.end("<!DOCTYPE html><html><body>Sign in - Google Accounts</body></html>"); }
  if (req.method === "GET" && url.pathname.startsWith("/r/")) {
    const o = responses[url.pathname.slice(3)]; if (!o) { res.writeHead(404); return res.end("gone"); }
    res.writeHead(200, { "Content-Type": o.mime }); return res.end(o.content);
  }
  if (req.method === "POST" && url.pathname === "/exec") {
    if (failFirst) { failFirst = false; res.writeHead(500, { "Content-Type": "text/html" }); return res.end("<html>Internal error</html>"); }
    let body = ""; req.on("data", d => body += d); req.on("end", () => {
      const o = sandbox.doPost({ postData: { contents: body, type: req.headers["content-type"] } });
      const id = String(++seq); responses[id] = o;
      res.writeHead(302, { Location: `http://127.0.0.1:${port}/r/${id}` }); res.end();
    });
    return;
  }
  res.writeHead(404); res.end("not found");
});
server.listen(port, "127.0.0.1", () => console.log(`fake Apps Script on http://127.0.0.1:${port}/exec`));
