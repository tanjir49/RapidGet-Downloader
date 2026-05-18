// popup.js — RapidGet Extension v2.0

const dot        = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const portInput  = document.getElementById("port-input");

// Load saved port
chrome.storage.local.get(["rapidget_port"], (res) => {
  if (res.rapidget_port) portInput.value = res.rapidget_port;
  checkConnection(portInput.value);
});

function checkConnection(port) {
  const ws = new WebSocket(`ws://127.0.0.1:${port}`);
  ws.onopen = () => {
    dot.className = "dot connected";
    statusText.textContent = `Connected — port ${port}`;
    ws.close();
  };
  ws.onerror = () => {
    dot.className = "dot error";
    statusText.textContent = `RapidGet not running (port ${port})`;
  };
  const timer = setTimeout(() => {
    dot.className = "dot error";
    statusText.textContent = "Connection timed out";
    ws.close();
  }, 3000);
  ws.onclose = () => clearTimeout(timer);
}

document.getElementById("send-tab").onclick = () => {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) {
      chrome.runtime.sendMessage({ type: "RAPIDGET_DOWNLOAD", url: tabs[0].url });
      statusText.textContent = "✅ Sent to RapidGet!";
    }
  });
};

document.getElementById("send-manual").onclick = () => {
  const url = document.getElementById("manual-url").value.trim();
  if (!url) return;
  chrome.runtime.sendMessage({ type: "RAPIDGET_DOWNLOAD", url });
  statusText.textContent = "✅ Sent to RapidGet!";
};

document.getElementById("save-port").onclick = () => {
  const port = parseInt(portInput.value) || 49152;
  chrome.runtime.sendMessage({ type: "SET_PORT", port }, () => {
    checkConnection(port);
  });
};
