// Buffers captures in chrome.storage and flushes them to the local tracker listener when it is up.
const LISTENER = "http://127.0.0.1:8765/capture";

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "capture") {
    chrome.storage.local.get({ buffer: [], total: 0 }, (st) => {
      st.buffer.push(msg.capture);
      chrome.storage.local.set({ buffer: st.buffer, total: st.total + 1, last: msg.capture }, flush);
    });
  }
});

async function flush() {
  const st = await chrome.storage.local.get({ buffer: [], sent: 0 });
  if (!st.buffer.length) return;
  try {
    const r = await fetch(LISTENER, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(st.buffer) });
    if (r.ok) {
      await chrome.storage.local.set({ buffer: [], sent: st.sent + st.buffer.length, lastFlush: new Date().toISOString() });
    }
  } catch (e) { /* listener not running: keep the buffer, retry on the next alarm */ }
}

chrome.alarms.create("flush", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "flush") flush(); });
chrome.runtime.onInstalled.addListener(flush);
