// Background service worker — relays messages between popup/content and the local API
const API = 'http://localhost:8000/api';

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'API_REQUEST') {
    handleApiRequest(msg.endpoint, msg.method, msg.body)
      .then(sendResponse)
      .catch(err => sendResponse({ error: err.message }));
    return true; // Keep channel open for async response
  }
});

async function handleApiRequest(endpoint, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${API}${endpoint}`, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}
