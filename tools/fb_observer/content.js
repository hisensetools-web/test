// Passive observer: watches the feed YOU are scrolling and records each Sponsored post once.
// It never scrolls, clicks, navigates or posts anything on Facebook. It only reads the DOM.
(function () {
  "use strict";
  const seen = new Set();
  const LISTENER = "http://127.0.0.1:8765/capture";

  function text(el) { return el ? (el.innerText || el.textContent || "").trim() : ""; }
  function num(s) {
    if (!s) return null;
    const m = String(s).replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*([KkMm])?/);
    if (!m) return null;
    let v = parseFloat(m[1]);
    if (m[2]) v *= m[2].toLowerCase() === "k" ? 1000 : 1000000;
    return Math.round(v);
  }

  // Facebook marks ad bodies with data-ad-preview / data-ad-comet-preview; the "Sponsored"
  // label is also a link/span. Either signal counts.
  function isSponsored(article) {
    if (article.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]')) return true;
    if (article.querySelector('a[aria-label="Sponsored"],span[aria-label="Sponsored"]')) return true;
    const spans = article.querySelectorAll("span,a");
    for (const s of spans) {
      const t = (s.textContent || "").replace(/\s+/g, "");
      if (t === "Sponsored" || t === "Sponsorisé" || t === "Patrocinado" || t === "Gesponsert") return true;
    }
    return false;
  }

  function permalinkOf(article) {
    // The timestamp link's href is often "#" until hovered; a synthetic mouseover fills it in.
    const links = Array.from(article.querySelectorAll("a[href]"));
    const pick = (l) => /\/posts\/|story_fbid=|\/videos\/|\/reel\/|\/photos\/|permalink\.php|\/watch\/\?v=/.test(l.href);
    let cand = links.filter(pick);
    if (!cand.length) {
      for (const l of links.slice(0, 40)) {
        if (l.getAttribute("href") === "#" || (l.getAttribute("href") || "").startsWith("/#")) {
          try { l.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); } catch (e) {}
        }
      }
      cand = Array.from(article.querySelectorAll("a[href]")).filter(pick);
    }
    return cand.length ? cand[0].href : null;
  }

  function pageOf(article) {
    const h = article.querySelector("h2,h3,h4");
    const a = h ? h.querySelector("a[href]") : article.querySelector('a[role="link"][href*="facebook.com/"]');
    const name = text(h && h.querySelector("strong,span") ? h.querySelector("strong,span") : a);
    const href = a ? a.href : null;
    let id = null;
    if (href) {
      const m = href.match(/profile\.php\?id=(\d+)/) || href.match(/facebook\.com\/(\d{6,})(?:[\/?]|$)/);
      if (m) id = m[1];
    }
    return { name: name || null, href, id };
  }

  function landingOf(article) {
    for (const l of article.querySelectorAll('a[href*="l.facebook.com/l.php"],a[href*="lm.facebook.com/l.php"]')) {
      try { const u = new URL(l.href).searchParams.get("u"); if (u) return u; } catch (e) {}
    }
    const ext = Array.from(article.querySelectorAll("a[href^='http']")).find((l) => !/facebook\.com|fb\.com|fbcdn/.test(l.hostname));
    return ext ? ext.href : null;
  }

  function countsOf(article) {
    const t = text(article);
    const grab = (re) => { const m = t.match(re); return m ? num(m[1]) : null; };
    let reactions = null;
    const r = article.querySelector('[aria-label*="reaction" i],[aria-label*="All reactions" i]');
    if (r) reactions = num(r.getAttribute("aria-label")) || num(text(r));
    if (reactions === null) reactions = grab(/(\d[\d,.]*[KkMm]?)\s+(?:reactions|likes)\b/i);
    return {
      reactions,
      comments: grab(/(\d[\d,.]*[KkMm]?)\s+comments?\b/i),
      shares: grab(/(\d[\d,.]*[KkMm]?)\s+shares?\b/i)
    };
  }

  function capture(article) {
    const body = article.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"],[dir="auto"]');
    const page = pageOf(article);
    const cap = {
      permalink: permalinkOf(article),
      page_name: page.name, page_id: page.id, page_url: page.href,
      primary_text: text(body).slice(0, 2000) || null,
      headline: null, landing_url: landingOf(article),
      image_url: (article.querySelector("img[src*='fbcdn']") || {}).src || null,
      ...countsOf(article),
      captured_at: new Date().toISOString(), url: location.href
    };
    const key = cap.permalink || ("feed:" + (cap.page_name || "") + "|" + (cap.primary_text || "").slice(0, 120));
    if (seen.has(key)) return;
    seen.add(key);
    if (!cap.permalink) cap.post_id = "feed_" + hash(key);
    chrome.runtime.sendMessage({ type: "capture", capture: cap });
  }

  function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }

  function scan() {
    document.querySelectorAll('[role="article"],div[aria-posinset]').forEach((a) => {
      if (a.dataset.estSeen) return;
      if (!isSponsored(a)) return;
      a.dataset.estSeen = "1";
      try { capture(a); } catch (e) { /* ignore */ }
    });
  }
  const obs = new MutationObserver(() => { clearTimeout(window.__estT); window.__estT = setTimeout(scan, 600); });
  obs.observe(document.documentElement, { childList: true, subtree: true });
  scan();
})();
