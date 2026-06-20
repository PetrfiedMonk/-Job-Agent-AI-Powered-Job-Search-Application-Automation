const API = "http://localhost:8000";
const POLL_ALARM = "poll-agent-status";

// Poll every 30 seconds
chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });

async function fetchStatus() {
  try {
    const res = await fetch(`${API}/api/status`, {
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) throw new Error("not ok");
    const d = await res.json();

    chrome.storage.local.set({
      agentStatus: d,
      connected: true,
      lastPoll: Date.now(),
    });

    // Badge: show applied count when running, clear when idle
    const applied = d.jobs_applied ?? 0;
    const text = d.is_running ? (applied > 0 ? String(applied) : "▶") : "";
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({
      color: d.is_running ? "#00d4ff" : "#1a2540",
    });
  } catch {
    chrome.storage.local.set({ connected: false, lastPoll: Date.now() });
    chrome.action.setBadgeText({ text: "" });
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) fetchStatus();
});

chrome.runtime.onInstalled.addListener(fetchStatus);
chrome.runtime.onStartup.addListener(fetchStatus);

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === "GET_STATUS") {
    chrome.storage.local.get(["agentStatus", "connected", "lastPoll"], reply);
    return true;
  }
  if (msg.type === "REFRESH") {
    fetchStatus().then(() => reply({ ok: true }));
    return true;
  }
});
