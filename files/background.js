// background.js — RapidGet Extension v2.0
// Relays URLs from content script / popup → RapidGet desktop app via WebSocket

const DEFAULT_PORT = 49152;
let ws = null;
let wsPort = DEFAULT_PORT;

// ── Restore saved port ────────────────────────────────────────────
chrome.storage.local.get(["rapidget_port"], (res) => {
  if (res.rapidget_port) wsPort = res.rapidget_port;
  connectWS();
});

// ── WebSocket connection ──────────────────────────────────────────
function connectWS() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  try {
    ws = new WebSocket(`ws://127.0.0.1:${wsPort}`);
    ws.onopen    = () => console.log("[RapidGet] WS connected on port", wsPort);
    ws.onclose   = () => setTimeout(connectWS, 5000);
    ws.onerror   = () => {};
  } catch (e) {}
}

// ── Send URL to app ───────────────────────────────────────────────
function sendUrl(url) {
  if (!url) return;
  function doSend() {
    ws.send(JSON.stringify({ url }));
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    doSend();
  } else {
    // Fallback: HTTP POST
    fetch(`http://127.0.0.1:${wsPort}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).catch(() => {});
    connectWS();
  }
}

// ── Listen from content scripts and popup ────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "RAPIDGET_DOWNLOAD") {
    sendUrl(msg.url);
    sendResponse({ ok: true });
  }
  if (msg.type === "SET_PORT") {
    wsPort = parseInt(msg.port) || DEFAULT_PORT;
    chrome.storage.local.set({ rapidget_port: wsPort });
    connectWS();
    sendResponse({ ok: true });
  }
  return true;
});

// ── Intercept .torrent downloads and redirect to app ─────────────
chrome.webRequest && chrome.webRequest.onHeadersReceived
  && chrome.webRequest.onHeadersReceived.addListener(
    (details) => {
      const ct = (details.responseHeaders || []).find(h =>
        h.name.toLowerCase() === "content-type"
      );
      if (ct && ct.value.includes("x-bittorrent")) {
        sendUrl(details.url);
        // Cancel the browser download
        return { cancel: true };
      }
    },
    { urls: ["<all_urls>"] },
    ["responseHeaders", "blocking"]
  );
