const API = "http://localhost:8000";
const $ = id => document.getElementById(id);

$("open-ui").onclick = () => chrome.tabs.create({ url: API });

function esc(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function renderOffline() {
  $("hdr-sub").textContent = "Not running";
  $("content").innerHTML = `
    <div class="offline-card">
      Job Agent is not running.<br><br>
      Start it with <strong>start_job_agent.bat</strong> (Windows)
      or <strong>./start_job_agent.sh</strong> (Mac/Linux),
      then <a id="retry-lnk">click here to retry</a>.
    </div>
    <div class="footer">
      <button class="foot-btn" id="retry-btn">↺ Retry</button>
    </div>
  `;
  const doRetry = () => chrome.runtime.sendMessage({ type: "REFRESH" }, init);
  $("retry-lnk")?.addEventListener("click", doRetry);
  $("retry-btn")?.addEventListener("click", doRetry);
}

function renderStatus(status, jobInfo) {
  const running = status?.is_running;
  const step = status?.current_step || (running ? "Running…" : "Idle");
  const key = running ? "running" : "idle";

  $("hdr-sub").textContent = running ? "Active" : "Ready";

  let html = `
    <div class="status-card">
      <div class="status-row">
        <div class="dot ${key}"></div>
        <div class="status-lbl ${key}">${running ? "◈ RUNNING" : "● IDLE"}</div>
        <div class="step-lbl">${esc(step)}</div>
      </div>
      <div class="stats">
        <div class="stat">
          <div class="stat-n">${status?.jobs_found ?? 0}</div>
          <div class="stat-l">Found</div>
        </div>
        <div class="stat">
          <div class="stat-n scored">${status?.jobs_scored ?? 0}</div>
          <div class="stat-l">Scored</div>
        </div>
        <div class="stat">
          <div class="stat-n applied">${status?.jobs_applied ?? 0}</div>
          <div class="stat-l">Applied</div>
        </div>
      </div>
    </div>
    <div class="divider"></div>
  `;

  if (jobInfo?.title) {
    html += `
      <div class="job-card">
        <div class="job-chip">📋 Job Detected</div>
        <div class="job-title">${esc(jobInfo.title)}</div>
        <div class="job-company">${esc(jobInfo.company || jobInfo.platform)}</div>
        <button class="track-btn" id="track-btn">⚡ Send to Job Agent</button>
        <div class="track-ok" id="track-ok">✓ Added to queue!</div>
      </div>
    `;
  }

  html += `
    <div class="footer">
      <button class="foot-btn" id="refresh-btn">↺ Refresh</button>
      <button class="foot-btn" id="dash-btn">Dashboard</button>
    </div>
  `;

  $("content").innerHTML = html;

  $("refresh-btn")?.addEventListener("click", () =>
    chrome.runtime.sendMessage({ type: "REFRESH" }, init)
  );
  $("dash-btn")?.addEventListener("click", () =>
    chrome.tabs.create({ url: API })
  );

  $("track-btn")?.addEventListener("click", async () => {
    const btn = $("track-btn");
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const res = await fetch(`${API}/api/track-job`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(jobInfo),
      });
      if (!res.ok) throw new Error("failed");
      $("track-ok").style.display = "block";
      btn.textContent = "✓ Sent!";
    } catch {
      btn.textContent = "✗ Agent not running";
      setTimeout(() => {
        btn.textContent = "⚡ Send to Job Agent";
        btn.disabled = false;
      }, 2500);
    }
  });
}

async function getTabJob() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]?.id) return resolve(null);
      chrome.tabs.sendMessage(tabs[0].id, { type: "GET_JOB" }, (resp) => {
        resolve(chrome.runtime.lastError ? null : resp);
      });
    });
  });
}

async function init() {
  const [stored, jobInfo] = await Promise.all([
    new Promise(r => chrome.runtime.sendMessage({ type: "GET_STATUS" }, r)),
    getTabJob(),
  ]);

  if (!stored?.connected) {
    renderOffline();
  } else {
    renderStatus(stored.agentStatus, jobInfo);
  }
}

init();
