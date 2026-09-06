function render() {
  chrome.storage.local.get({ buffer: [], total: 0, sent: 0, last: null, lastFlush: null }, (st) => {
    document.getElementById("total").textContent = st.total;
    document.getElementById("sent").textContent = st.sent;
    document.getElementById("buf").textContent = st.buffer.length;
    document.getElementById("flush").textContent = st.lastFlush || "never";
    document.getElementById("last").textContent = st.last ? JSON.stringify(st.last, null, 1).slice(0, 1500) : "-";
  });
}
document.getElementById("export").onclick = () => {
  chrome.storage.local.get({ buffer: [] }, (st) => {
    const blob = new Blob([JSON.stringify(st.buffer, null, 1)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "captures.json"; a.click();
  });
};
document.getElementById("flushBtn").onclick = async () => {
  const st = await chrome.storage.local.get({ buffer: [], sent: 0 });
  if (!st.buffer.length) return render();
  try {
    const r = await fetch("http://127.0.0.1:8765/capture", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(st.buffer) });
    if (r.ok) await chrome.storage.local.set({ buffer: [], sent: st.sent + st.buffer.length, lastFlush: new Date().toISOString() });
    else alert("tracker answered HTTP " + r.status);
  } catch (e) { alert("tracker listener not running: start `python tracker.py fb-listen`"); }
  render();
};
document.getElementById("clear").onclick = () => chrome.storage.local.set({ buffer: [] }, render);
render();
