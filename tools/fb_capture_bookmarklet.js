// Bookmarklet: while looking at a Sponsored post you opened BY HAND (click its timestamp so the
// permalink is in the address bar), click this bookmark. It copies a JSON capture to the clipboard.
// Then run:  python tracker.py fb-capture --paste
// Install: create a bookmark whose URL is "javascript:" followed by the minified line at the bottom.
(function () {
  const txt = document.body.innerText || "";
  const num = (s) => s ? s.replace(/,/g, "") : null;
  const grab = (re) => { const m = txt.match(re); return m ? num(m[1]) : null; };
  const sel = (window.getSelection && String(window.getSelection())) || "";
  const cap = {
    permalink: location.href,
    page_name: (document.title || "").split(" | ")[0].replace(/ - Facebook$/, "").trim(),
    primary_text: sel.trim() || null,           // select the ad copy before clicking for an exact match
    reactions: grab(/(\d[\d,.]*[KkMm]?)\s+(?:reactions|likes)\b/i) || grab(/All reactions:\s*(\d[\d,.]*[KkMm]?)/i),
    comments: grab(/(\d[\d,.]*[KkMm]?)\s+comments?\b/i),
    shares: grab(/(\d[\d,.]*[KkMm]?)\s+shares?\b/i),
    captured_at: new Date().toISOString()
  };
  const s = JSON.stringify(cap);
  (navigator.clipboard ? navigator.clipboard.writeText(s) : Promise.reject()).then(
    () => alert("Captured: " + s.slice(0, 200)), () => prompt("Copy this:", s));
})();
// minified:
// javascript:(function(){const t=document.body.innerText||"",n=s=>s?s.replace(/,/g,""):null,g=r=>{const m=t.match(r);return m?n(m[1]):null},l=(window.getSelection&&String(window.getSelection()))||"",c={permalink:location.href,page_name:(document.title||"").split(" | ")[0].replace(/ - Facebook$/,"").trim(),primary_text:l.trim()||null,reactions:g(/(\d[\d,.]*[KkMm]?)\s+(?:reactions|likes)\b/i)||g(/All reactions:\s*(\d[\d,.]*[KkMm]?)/i),comments:g(/(\d[\d,.]*[KkMm]?)\s+comments?\b/i),shares:g(/(\d[\d,.]*[KkMm]?)\s+shares?\b/i),captured_at:new Date().toISOString()},s=JSON.stringify(c);(navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(()=>alert("Captured: "+s.slice(0,200)),()=>prompt("Copy this:",s))})();
