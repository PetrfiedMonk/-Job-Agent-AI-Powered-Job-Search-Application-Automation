const API = 'http://localhost:8000/api';

// ── API helpers ──────────────────────────────────────────────────────────────

async function apiFetch(endpoint, opts = {}) {
  const res = await fetch(`${API}${endpoint}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── UI refs ──────────────────────────────────────────────────────────────────

const connDot    = document.getElementById('conn-dot');
const connLabel  = document.getElementById('conn-label');
const connDetail = document.getElementById('conn-detail');
const profileCard = document.getElementById('profile-card');
const profileName = document.getElementById('profile-name');
const profileTitle = document.getElementById('profile-title');
const statsGrid  = document.getElementById('stats-grid');
const statSites  = document.getElementById('stat-sites');
const statFills  = document.getElementById('stat-fills');
const fillBtn    = document.getElementById('fill-btn');
const ksList     = document.getElementById('ks-list');
const knowCard   = document.getElementById('known-sites-card');
const openDash   = document.getElementById('open-dashboard-btn');
const refreshBtn = document.getElementById('refresh-btn');

// ── Main load ────────────────────────────────────────────────────────────────

async function load() {
  // 1. Check server health
  try {
    await apiFetch('/health');
    connDot.className = 'dot dot-ok';
    connLabel.textContent = 'Connected';
    connDetail.textContent = 'Job Agent running on localhost:8000';
  } catch {
    connDot.className = 'dot dot-err';
    connLabel.textContent = 'Not connected';
    connDetail.textContent = 'Start the Job Agent server to use Smart Apply';
    fillBtn.disabled = true;
    return;
  }

  // 2. Load everything in parallel
  const [profileResult, intelResult, sitesResult] = await Promise.allSettled([
    apiFetch('/profile'),
    apiFetch('/field-intelligence'),
    apiFetch('/known-sites'),
  ]);

  // Profile
  if (profileResult.status === 'fulfilled') {
    const d = profileResult.value;
    if (d.built) {
      profileCard.style.display = 'flex';
      profileName.textContent  = d.name || 'Your Profile';
      profileTitle.textContent = d.current_title || d.summary?.slice(0, 60) || 'Profile ready';
      fillBtn.disabled = false;
    } else {
      profileName.textContent  = 'Profile not built yet';
      profileTitle.textContent = 'Open the dashboard to build your profile';
    }
  }

  // Global field intelligence
  if (intelResult.status === 'fulfilled') {
    const d = intelResult.value || {};
    statsGrid.style.display = 'grid';
    statSites.textContent = d.known_field_fingerprints || 0;
    statFills.textContent = d.total_successful_fills   || 0;
    document.getElementById('stat-sites-lbl').textContent = 'Fields Learned';
    document.getElementById('stat-fills-lbl').textContent = 'Total Fills';
  }

  // Known sites
  if (sitesResult.status === 'fulfilled') {
    const sites = sitesResult.value || [];
    if (sites.length > 0) {
      knowCard.style.display = 'block';
      ksList.innerHTML = sites.slice(0, 8).map(s => `
        <div class="ks-row">
          <div class="ks-dot"></div>
          <div class="ks-domain">${s.domain}</div>
          <div class="ks-count">${s.total_fills || s.submissions || 0} fills</div>
        </div>
      `).join('');
    }
  }
}

// ── Smart Fill current tab ───────────────────────────────────────────────────

fillBtn.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;

  // Tell content script to open its panel (it handles the actual fill)
  await chrome.tabs.sendMessage(tab.id, { type: 'OPEN_PANEL' }).catch(() => {
    // Content script may not be loaded on this page — inject it
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ['content.js'],
    }).then(() => {
      setTimeout(() => {
        chrome.tabs.sendMessage(tab.id, { type: 'OPEN_PANEL' });
      }, 300);
    });
  });

  window.close();
});

// ── Open dashboard ───────────────────────────────────────────────────────────

openDash.addEventListener('click', () => {
  chrome.tabs.create({ url: 'http://localhost:5173' });
});

// ── Refresh ──────────────────────────────────────────────────────────────────

refreshBtn.addEventListener('click', () => {
  ksList.innerHTML = '<div class="ks-empty">Loading…</div>';
  load();
});

// ── Init ─────────────────────────────────────────────────────────────────────

load();
