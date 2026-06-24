/**
 * Job Agent Smart Apply — Content Script
 *
 * Two-phase approach matches the backend:
 *  Phase 1 — Classify: backend tells us what each field IS (global knowledge)
 *  Phase 2 — Answer:   backend returns values; question types get AI-written text
 *
 * Panel lets user review + edit question answers before filling.
 * On submit, corrections are sent back so the system learns.
 */
(function () {
  'use strict';
  if (document.getElementById('__ja-root__')) return;

  // ── State ──────────────────────────────────────────────────────────────────
  let fills = [];
  let editedAnswers = {};
  let jobContext = null;
  let _submitLogged = false;

  const DOMAIN = window.location.hostname.replace(/^www\./, '');

  // ── Category metadata ──────────────────────────────────────────────────────
  const CATEGORIES = {
    personal:     { label: 'Personal Info',        icon: '👤', color: '#22d3ee' },
    social:       { label: 'Online Presence',       icon: '🔗', color: '#818cf8' },
    work_auth:    { label: 'Work Authorization',    icon: '✅', color: '#34d399' },
    compliance:   { label: 'Compliance',            icon: '📋', color: '#34d399' },
    compensation: { label: 'Compensation',          icon: '💰', color: '#f59e0b' },
    experience:   { label: 'Experience',            icon: '🎯', color: '#fb923c' },
    file:         { label: 'File Uploads',          icon: '📎', color: '#a78bfa' },
    question:     { label: 'Application Questions', icon: '✍️', color: '#f472b6' },
    unknown:      { label: 'Other Fields',          icon: '❓', color: '#475569' },
  };

  // ── DOM helpers ────────────────────────────────────────────────────────────

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = window.getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
  }

  function getFieldLabel(el) {
    const aria = el.getAttribute('aria-label');
    if (aria?.trim()) return aria.trim();
    const lblBy = el.getAttribute('aria-labelledby');
    if (lblBy) {
      const t = lblBy.split(' ').map(id => document.getElementById(id)?.textContent?.trim()).filter(Boolean).join(' ');
      if (t) return t;
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.replace(/\*/g, '').trim();
    }
    const parent = el.closest('label');
    if (parent) {
      const clone = parent.cloneNode(true);
      clone.querySelectorAll('input,select,textarea').forEach(c => c.remove());
      return clone.textContent.replace(/\*/g, '').trim();
    }
    const prev = el.previousElementSibling;
    if (prev && ['LABEL','SPAN','DIV','P','LEGEND','H1','H2','H3','H4'].includes(prev.tagName)) {
      const t = prev.textContent.replace(/\*/g, '').trim();
      if (t.length < 120) return t;
    }
    return el.placeholder || el.name || el.id || '';
  }

  function scanFields() {
    const seen = new Set();
    const out = [];
    const sel = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]),textarea,select';
    for (const el of document.querySelectorAll(sel)) {
      if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
      if (!el.dataset.jaId) el.dataset.jaId = 'ja_' + Math.random().toString(36).slice(2, 9);
      if (seen.has(el.dataset.jaId)) continue;
      seen.add(el.dataset.jaId);
      const type = (el.type || el.tagName).toLowerCase();
      out.push({
        id: el.dataset.jaId,
        label: getFieldLabel(el),
        name: el.name || '',
        type,
        placeholder: el.placeholder || '',
        options: type === 'select' ? [...el.options].map(o => o.text.trim()).filter(Boolean) : [],
        required: el.required || el.getAttribute('aria-required') === 'true',
        current_value: el.value || '',
      });
    }
    return out;
  }

  // ── React-compatible field fill ────────────────────────────────────────────

  function fillField(id, value) {
    const el = document.querySelector(`[data-ja-id="${id}"]`);
    if (!el || !value || value === '__resume__' || value === '__file__') return false;
    const type = (el.type || el.tagName).toLowerCase();

    if (type === 'select') {
      const opts = [...el.options];
      const match =
        opts.find(o => o.value.toLowerCase() === value.toLowerCase()) ||
        opts.find(o => o.text.toLowerCase() === value.toLowerCase()) ||
        opts.find(o => o.text.toLowerCase().includes(value.toLowerCase())) ||
        (/^yes$/i.test(value) && opts.find(o => /yes|true|1/i.test(o.text))) ||
        (/^no$/i.test(value)  && opts.find(o => /no|false|0/i.test(o.text)));
      if (match) { el.value = match.value; el.dispatchEvent(new Event('change', {bubbles:true})); }
      return true;
    }
    if (type === 'checkbox') { if (el.checked !== /^(yes|true|1|on)$/i.test(value)) el.click(); return true; }
    if (type === 'radio') {
      const radios = document.querySelectorAll(`input[type=radio][name="${el.name}"]`);
      for (const r of radios) {
        const lbl = getFieldLabel(r).toLowerCase(), val = r.value.toLowerCase();
        if (val === value.toLowerCase() || lbl.includes(value.toLowerCase()) ||
            (/^yes$/i.test(value) && /yes|true/i.test(val+lbl)) ||
            (/^no$/i.test(value)  && /no|false/i.test(val+lbl))) { r.click(); break; }
      }
      return true;
    }

    // text/email/tel/textarea — React-compatible via native setter
    try {
      const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value); else el.value = value;
    } catch { el.value = value; }
    el.dispatchEvent(new Event('input',  {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));

    const prev = el.style.outline;
    el.style.outline = '2px solid #22d3ee';
    setTimeout(() => { el.style.outline = prev; }, 900);
    return true;
  }

  // ── Job context detection ──────────────────────────────────────────────────

  function detectJobContext() {
    const title = (
      document.querySelector('h1')?.textContent ||
      document.querySelector('[class*="job-title"],[class*="jobTitle"]')?.textContent ||
      document.title
    )?.trim().slice(0, 120);
    const company = (
      document.querySelector('[class*="company-name"],[class*="companyName"],[class*="employer"]')?.textContent ||
      DOMAIN
    )?.trim().slice(0, 80);
    return { title: title || '', company: company || DOMAIN, url: location.href };
  }

  function isJobPage() {
    const url = location.href.toLowerCase();
    const signals = ['/apply','/application','/jobs/','/careers/','/job/','/position/',
                     'greenhouse.io','lever.co','workday.com','myworkdayjobs',
                     'icims.com','taleo.net','smartrecruiters.com','jobvite.com',
                     'bamboohr.com','ashbyhq.com','rippling.com'];
    if (signals.some(s => url.includes(s))) return true;
    return !!(document.querySelector('input[name*="name" i],input[placeholder*="name" i]') &&
              document.querySelector('input[type=email],input[name*="email" i]'));
  }

  // ── Auto-log application on submit ────────────────────────────────────────

  async function logApplication() {
    if (_submitLogged) return;
    _submitLogged = true;
    const ctx = jobContext || detectJobContext();
    try {
      const res = await apiCall('/extension/log-application', 'POST', {
        url: ctx.url, title: ctx.title, company: ctx.company,
        fields_filled: fills.filter(f => f.value && f.value !== '__resume__' && f.value !== '__file__').length,
      });
      if (shadow) {
        const body = shadow.getElementById('body');
        if (body) {
          const el = document.createElement('div');
          el.style.cssText = 'background:rgba(16,185,129,.08);border:1px solid rgba(16,185,129,.2);border-radius:8px;padding:9px 12px;font-size:11px;color:#34d399;text-align:center;margin-bottom:4px';
          el.textContent = res.is_new_job ? '✓ New job added & application tracked' : '✓ Application logged in your pipeline';
          body.prepend(el);
          setStatus(shadow, 'd-ok', 'Application logged ✓', 'b-instant', '✓ tracked');
        }
      }
    } catch (_e) {
      _submitLogged = false; // allow retry if backend was down
    }
  }

  function setupSubmitWatcher() {
    // Real form submits (standard ATS systems)
    document.addEventListener('submit', () => {
      if (fills.length > 0) logApplication();
    }, true);

    // SPA submit buttons (LinkedIn Easy Apply, Greenhouse, Lever, etc.)
    document.addEventListener('click', (e) => {
      if (fills.length === 0 || _submitLogged) return;
      const el = e.target.closest('button,[type=submit]');
      if (!el) return;
      const text = (el.textContent || el.value || '').toLowerCase().trim();
      if (/\b(submit application|submit|apply now|send application|complete application)\b/.test(text)) {
        logApplication();
      }
    }, true);
  }

  // ── Fit Radar — Job Listing Discovery Mode ────────────────────────────────

  function isJobListingPage() {
    const u = location.href.toLowerCase();
    return (
      (u.includes('linkedin.com')     && (u.includes('/jobs/search') || u.includes('/jobs/collections'))) ||
      (u.includes('indeed.com')       && (u.includes('/jobs?')       || u.includes('/jobs/'))) ||
      (u.includes('glassdoor.com')    && (u.includes('/job-listing') || u.includes('/jobs/'))) ||
      (u.includes('ziprecruiter.com') && u.includes('/jobs'))
    );
  }

  function extractJobCards() {
    const u = location.href.toLowerCase();
    const cards = [];

    if (u.includes('linkedin.com')) {
      for (const el of document.querySelectorAll(
        'li.jobs-search-results__list-item, li.scaffold-layout__list-item'
      )) {
        const titleEl   = el.querySelector('a.job-card-list__title, .job-card-list__title--link, .base-search-card__title a, .base-search-card__title');
        const companyEl = el.querySelector('.job-card-container__company-name, .artdeco-entity-lockup__subtitle, .base-search-card__subtitle');
        const locEl     = el.querySelector('.job-card-container__metadata-item, .job-search-card__location');
        const linkEl    = el.querySelector('a[href*="/jobs/view"]') || titleEl;
        if (!titleEl || !linkEl) continue;
        cards.push({
          el,
          url:      (linkEl.href || '').split('?')[0],
          title:    titleEl.textContent.trim(),
          company:  (companyEl?.textContent || '').trim(),
          location: (locEl?.textContent || '').trim(),
        });
      }
    } else if (u.includes('indeed.com')) {
      for (const el of document.querySelectorAll('.job_seen_beacon, [data-jk]')) {
        const titleEl   = el.querySelector('h2.jobTitle a, .jobTitle a, h2.jobTitle');
        const companyEl = el.querySelector('[data-testid="company-name"], .companyName');
        const locEl     = el.querySelector('[data-testid="text-location"], .companyLocation');
        const jk        = el.getAttribute('data-jk');
        if (!titleEl) continue;
        cards.push({
          el,
          url:      jk ? `https://www.indeed.com/viewjob?jk=${jk}` : (titleEl.href || ''),
          title:    titleEl.textContent.trim(),
          company:  (companyEl?.textContent || '').trim(),
          location: (locEl?.textContent || '').trim(),
        });
      }
    } else if (u.includes('glassdoor.com')) {
      for (const el of document.querySelectorAll('li[data-test="jobListing"], [class*="JobCard_jobCard"]')) {
        const titleEl   = el.querySelector('a[data-test="job-title"], [class*="JobCard_seoLink"]');
        const companyEl = el.querySelector('[class*="EmployerProfile_profileContainer"], [data-test="employer-name"]');
        if (!titleEl) continue;
        cards.push({
          el,
          url:      (titleEl.href || '').split('?')[0],
          title:    titleEl.textContent.trim(),
          company:  (companyEl?.textContent || '').trim(),
          location: '',
        });
      }
    } else if (u.includes('ziprecruiter.com')) {
      for (const el of document.querySelectorAll('article.job_result, .job_result')) {
        const titleEl   = el.querySelector('.job_result_title a');
        const companyEl = el.querySelector('.hiring_company_text, .t_org_link');
        if (!titleEl) continue;
        cards.push({
          el,
          url:      titleEl.href || '',
          title:    titleEl.textContent.trim(),
          company:  (companyEl?.textContent || '').trim(),
          location: '',
        });
      }
    }

    return cards;
  }

  function injectBadge(cardEl, result) {
    if (cardEl.querySelector('[data-ja-radar]')) return;
    const s     = result.score;
    const color = s >= 80 ? '#22d3ee' : s >= 60 ? '#34d399' : s >= 40 ? '#f59e0b' : '#ef4444';
    const tier  = s >= 80 ? '🔥 Hot'  : s >= 60 ? '✓ Fit'   : s >= 40 ? '~ Weak'  : '✗ Skip';

    const wrap = document.createElement('div');
    wrap.setAttribute('data-ja-radar', 'true');
    wrap.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:5px;flex-wrap:wrap;';

    const badge = document.createElement('span');
    badge.style.cssText = `display:inline-flex;align-items:center;gap:4px;background:rgba(13,22,40,.93);border:1px solid ${color}55;border-radius:5px;padding:3px 8px;font-family:Inter,system-ui,sans-serif;font-size:10px;font-weight:700;color:${color};box-shadow:0 1px 6px rgba(0,0,0,.3);`;
    badge.innerHTML = `<span>${tier}</span><span style="color:#94a3b8;font-weight:400;font-size:9px">${s}/100</span>`;
    if (result.matched_keywords?.length) {
      const kw = document.createElement('span');
      kw.style.cssText = 'color:#64748b;font-size:9px;font-weight:400;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      kw.title = result.matched_keywords.join(', ');
      kw.textContent = '· ' + result.matched_keywords[0];
      badge.appendChild(kw);
    }

    const addBtn = document.createElement('button');
    addBtn.style.cssText = 'background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);color:#818cf8;border-radius:4px;padding:2px 8px;font-size:10px;font-weight:700;cursor:pointer;font-family:Inter,system-ui,sans-serif;line-height:1.5;';
    addBtn.textContent = '+ Pipeline';
    addBtn.onclick = async (e) => {
      e.stopPropagation(); e.preventDefault();
      addBtn.disabled = true;
      addBtn.textContent = '…';
      try {
        const r = await apiCall('/jobs/add-to-pipeline', 'POST', {
          url: result.url, title: result.title, company: result.company,
          location: result.location, score: result.score,
        });
        addBtn.textContent = r.is_new ? '✓ Added' : '✓ Tracked';
        addBtn.style.color = '#34d399';
        addBtn.style.borderColor = 'rgba(52,211,153,.3)';
        addBtn.style.background  = 'rgba(52,211,153,.08)';
      } catch (_e) {
        addBtn.textContent = '✗ Error';
        addBtn.disabled = false;
      }
    };

    wrap.appendChild(badge);
    wrap.appendChild(addBtn);

    const anchor = cardEl.querySelector('.job-card-list__title, .base-search-card__title, h2.jobTitle, [data-test="job-title"], .job_result_title, h2, h3') || cardEl;
    if (anchor.after) anchor.after(wrap);
    else if (anchor.parentNode) anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
  }

  let _radarRunning = false;
  async function injectFitRadar() {
    if (_radarRunning) return;
    _radarRunning = true;
    try {
      const cards = extractJobCards();
      if (!cards.length) return;

      const fresh = cards.filter(c => !c.el.querySelector('[data-ja-radar]') && c.url);
      if (!fresh.length) return;

      const jobs = fresh.map(c => ({ url: c.url, title: c.title, company: c.company, location: c.location, description: '' }));
      const res  = await apiCall('/jobs/score-preview', 'POST', { jobs });

      const byUrl = {};
      for (const r of (res.results || [])) byUrl[r.url] = r;

      for (const card of fresh) {
        const result = byUrl[card.url];
        if (result) injectBadge(card.el, result);
      }

      if (shadow) {
        const total = cards.length;
        const hot   = (res.results || []).filter(r => r.score >= 80).length;
        setStatus(shadow, 'd-ok',
          `Fit Radar: ${total} jobs scored${hot ? ` · ${hot} hot` : ''}`,
          'b-instant', `${total} scanned`
        );
        const body = shadow.getElementById('body');
        if (body) {
          body.innerHTML = `
            <div style="text-align:center;padding:18px 12px;display:flex;flex-direction:column;gap:10px;align-items:center">
              <div style="font-size:26px">🎯</div>
              <div style="font-size:13px;font-weight:700;color:#e2e8f0">Fit Radar Active</div>
              <div style="font-size:11px;color:#64748b;line-height:1.6">${total} jobs scored · click <strong style="color:#818cf8">+ Pipeline</strong> on any card to track it</div>
              ${hot ? `<div style="background:rgba(34,211,238,.06);border:1px solid rgba(34,211,238,.15);border-radius:8px;padding:8px 14px;font-size:11px;color:#22d3ee;font-weight:700">${hot} 🔥 hot match${hot === 1 ? '' : 'es'} on this page</div>` : ''}
            </div>`;
        }
        // Swap footer buttons for listing mode
        const fillBtn  = shadow.getElementById('fill-btn');
        const learnBtn = shadow.getElementById('learn-btn');
        if (fillBtn) {
          fillBtn.textContent = '🔄 Rescan Page';
          fillBtn.onclick = () => { _radarRunning = false; injectFitRadar(); };
        }
        if (learnBtn) learnBtn.style.display = 'none';
      }
    } catch (_e) {
      // silently fail — server may not be running
    } finally {
      _radarRunning = false;
    }
  }

  function setupListingWatcher() {
    let _bounce;
    const obs = new MutationObserver(() => {
      clearTimeout(_bounce);
      _bounce = setTimeout(() => { _radarRunning = false; injectFitRadar(); }, 700);
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // ── API bridge ─────────────────────────────────────────────────────────────

  function apiCall(endpoint, method = 'GET', body = null) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: 'API_REQUEST', endpoint, method, body }, res => {
        if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
        if (res?.error) return reject(new Error(res.error));
        resolve(res);
      });
    });
  }

  // ── Panel CSS ──────────────────────────────────────────────────────────────

  const CSS_TEXT = `
    :host { all:initial; font-family:'Inter',system-ui,sans-serif; }
    *,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
    #panel {
      position:fixed; bottom:22px; right:22px; z-index:2147483647;
      width:390px; max-height:88vh;
      background:#0d1628; border:1px solid rgba(34,211,238,.18); border-radius:16px;
      box-shadow:0 28px 90px rgba(0,0,0,.8),0 0 50px rgba(34,211,238,.06);
      display:flex; flex-direction:column; font-size:13px; color:#e2e8f0;
      transition:transform .3s cubic-bezier(.4,0,.2,1),opacity .3s;
    }
    #panel.hidden { transform:translateY(130%); opacity:0; pointer-events:none; }
    .hd {
      background:linear-gradient(90deg,rgba(34,211,238,.07),rgba(99,102,241,.05));
      border-bottom:1px solid rgba(34,211,238,.1); padding:12px 14px;
      display:flex; align-items:center; gap:9px; flex-shrink:0;
    }
    .logo { width:30px; height:30px; background:linear-gradient(135deg,#6366f1,#22d3ee);
      border-radius:8px; display:flex; align-items:center; justify-content:center;
      font-size:14px; flex-shrink:0; }
    .hd-text { flex:1; }
    .hd-title { font-size:12px; font-weight:700; color:#e2e8f0; }
    .hd-sub { font-size:10px; color:#475569; }
    .x { background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.07);
      border-radius:5px; width:20px; height:20px; cursor:pointer; color:#64748b;
      font-size:10px; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
    .x:hover { background:rgba(255,255,255,.09); color:#94a3b8; }
    .sb { padding:8px 14px; display:flex; align-items:center; gap:8px;
      border-bottom:1px solid rgba(255,255,255,.05); flex-shrink:0; }
    .dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
    .d-idle { background:#1e3a5f; }
    .d-ok { background:#22d3ee; box-shadow:0 0 8px rgba(34,211,238,.5); animation:pulse 2s infinite; }
    .d-warn { background:#f59e0b; }
    .d-err { background:#ef4444; }
    @keyframes pulse{0%,100%{box-shadow:0 0 5px rgba(34,211,238,.35)}50%{box-shadow:0 0 14px rgba(34,211,238,.7)}}
    .st { flex:1; font-size:11px; color:#64748b; }
    .bdg { font-size:9px; font-weight:800; padding:2px 6px; border-radius:10px;
      flex-shrink:0; text-transform:uppercase; letter-spacing:.05em; }
    .b-instant { background:rgba(16,185,129,.1); color:#34d399; border:1px solid rgba(16,185,129,.2); }
    .b-ai { background:rgba(99,102,241,.1); color:#818cf8; border:1px solid rgba(99,102,241,.2); }
    .body { flex:1; overflow-y:auto; padding:12px 14px; display:flex; flex-direction:column; gap:10px; }
    .body::-webkit-scrollbar { width:3px; }
    .body::-webkit-scrollbar-thumb { background:rgba(255,255,255,.07); border-radius:2px; }
    .job-ctx { font-size:10px; color:#475569; padding:6px 10px;
      background:rgba(255,255,255,.02); border-radius:7px; border:1px solid rgba(255,255,255,.05); }
    .job-ctx strong { color:#64748b; }
    .cat { display:flex; flex-direction:column; gap:4px; }
    .cat-hdr { display:flex; align-items:center; gap:6px; font-size:10px; font-weight:700;
      text-transform:uppercase; letter-spacing:.07em; color:#1e3a5f; padding:2px 0; }
    /* Standard rows */
    .frow { background:rgba(255,255,255,.025); border:1px solid rgba(255,255,255,.06);
      border-radius:8px; padding:7px 10px; display:flex; align-items:center; gap:8px; }
    .fl { font-size:10px; color:#475569; flex-shrink:0; width:100px;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .fv { flex:1; font-size:11px; color:#94a3b8; white-space:nowrap;
      overflow:hidden; text-overflow:ellipsis; min-width:0; }
    .fsrc { font-size:9px; font-weight:700; flex-shrink:0; padding:1px 5px;
      border-radius:4px; text-transform:uppercase; letter-spacing:.04em; }
    .s-l { background:rgba(16,185,129,.1); color:#34d399; }
    .s-s { background:rgba(34,211,238,.08); color:#22d3ee; }
    .s-a { background:rgba(99,102,241,.1); color:#818cf8; }
    .conf { height:2px; background:rgba(255,255,255,.04); border-radius:1px; margin-top:3px; overflow:hidden; }
    .cf { height:100%; border-radius:1px; }
    /* Question rows */
    .qrow { background:rgba(244,114,182,.04); border:1px solid rgba(244,114,182,.15);
      border-radius:10px; padding:10px; display:flex; flex-direction:column; gap:7px; }
    .qtop { display:flex; align-items:center; gap:7px; }
    .ql { font-size:10px; color:#f472b6; font-weight:700; flex:1;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .qsrc { font-size:9px; font-weight:700; padding:1px 5px; border-radius:4px; text-transform:uppercase; }
    .qs-ai { background:rgba(244,114,182,.1); color:#f472b6; }
    .qs-l { background:rgba(16,185,129,.1); color:#34d399; }
    .qs-c { background:rgba(245,158,11,.1); color:#f59e0b; }
    .qedit { font-size:9px; color:#64748b; cursor:pointer; padding:2px 6px;
      background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.07);
      border-radius:4px; flex-shrink:0; }
    .qedit:hover { color:#94a3b8; background:rgba(255,255,255,.07); }
    .qfill-btn { display:none; align-self:flex-end; font-size:10px; font-weight:700;
      padding:4px 10px; background:rgba(244,114,182,.1); border:1px solid rgba(244,114,182,.2);
      border-radius:6px; color:#f472b6; cursor:pointer; }
    .qfill-btn.open { display:inline-flex; }
    .qprev { font-size:11px; color:#64748b; line-height:1.5;
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
    .qta { display:none; width:100%; min-height:90px; background:rgba(255,255,255,.03);
      border:1px solid rgba(244,114,182,.2); border-radius:7px; padding:8px;
      font-size:11px; color:#cbd5e1; resize:vertical; font-family:inherit;
      line-height:1.5; outline:none; }
    .qta:focus { border-color:rgba(244,114,182,.4); }
    .qta.open { display:block; }
    /* Fill-now button for questions */
    .qfill-now { display:none; align-self:flex-end; font-size:10px; font-weight:700;
      padding:5px 12px; background:linear-gradient(135deg,rgba(244,114,182,.2),rgba(99,102,241,.2));
      border:1px solid rgba(244,114,182,.3); border-radius:7px; color:#f472b6; cursor:pointer; }
    .qfill-now.visible { display:inline-flex; }
    /* Upload rows */
    .urow { background:rgba(167,139,250,.05); border:1px solid rgba(167,139,250,.15);
      border-radius:8px; padding:8px 10px; display:flex; align-items:center; gap:8px; }
    .ubdg { font-size:9px; font-weight:700; padding:2px 6px; border-radius:4px;
      background:rgba(167,139,250,.1); color:#a78bfa; border:1px solid rgba(167,139,250,.2);
      text-transform:uppercase; }
    /* Footer */
    .ft { flex-shrink:0; padding:10px 14px; border-top:1px solid rgba(255,255,255,.05);
      display:flex; flex-direction:column; gap:6px; }
    .btn { display:flex; align-items:center; justify-content:center; gap:6px;
      width:100%; padding:9px; border:none; border-radius:9px; font-size:12px;
      font-weight:700; cursor:pointer; font-family:inherit; letter-spacing:.02em; transition:all .15s; }
    .btn:disabled { opacity:.35; cursor:not-allowed; }
    .btn-fill { background:linear-gradient(135deg,#06b6d4,#6366f1); color:#fff;
      box-shadow:0 0 20px rgba(34,211,238,.15); }
    .btn-fill:hover:not(:disabled) { box-shadow:0 0 36px rgba(34,211,238,.3); transform:translateY(-1px); }
    .btn-learn { background:rgba(16,185,129,.08); color:#34d399; border:1px solid rgba(16,185,129,.18); }
    .btn-learn:hover:not(:disabled) { background:rgba(16,185,129,.15); }
    .empty { text-align:center; color:#1e3a5f; font-size:11px; padding:20px; }
    .intel { font-size:10px; color:#1e3a5f; text-align:center; padding:4px; }
  `;

  // ── Shadow DOM panel ───────────────────────────────────────────────────────

  let shadow = null;
  let panelVisible = false;

  function createShadow() {
    const host = document.createElement('div');
    host.id = '__ja-root__';
    host.style.cssText = 'all:initial;position:fixed;bottom:0;right:0;z-index:2147483647;';
    document.body.appendChild(host);

    const sh = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = CSS_TEXT;
    sh.appendChild(style);

    const panel = document.createElement('div');
    panel.id = 'panel';
    panel.classList.add('hidden');
    panel.innerHTML = `
      <div class="hd">
        <div class="logo">⚡</div>
        <div class="hd-text">
          <div class="hd-title">Job Agent Smart Apply</div>
          <div class="hd-sub">${DOMAIN}</div>
        </div>
        <div class="x" id="xbtn">✕</div>
      </div>
      <div class="sb">
        <div class="dot d-idle" id="dot"></div>
        <div class="st" id="st">Ready</div>
        <span id="bdg" class="bdg" style="display:none"></span>
      </div>
      <div class="body" id="body">
        <div class="empty">Click <strong>Smart Fill</strong> to analyze this form.</div>
      </div>
      <div class="ft">
        <button class="btn btn-fill" id="fill-btn">⚡ Smart Fill This Form</button>
        <button class="btn btn-learn" id="learn-btn" style="display:none">✓ Submitted — Save What Worked</button>
      </div>
    `;
    sh.appendChild(panel);

    sh.getElementById('xbtn').onclick     = () => toggle(false);
    sh.getElementById('fill-btn').onclick = () => doFill(sh);
    sh.getElementById('learn-btn').onclick= () => doLearn(sh);

    return sh;
  }

  function setStatus(sh, dotCls, text, bdgCls, bdgText) {
    sh.getElementById('dot').className = 'dot ' + dotCls;
    sh.getElementById('st').textContent = text;
    const b = sh.getElementById('bdg');
    if (bdgCls && bdgText) { b.className = 'bdg ' + bdgCls; b.textContent = bdgText; b.style.display = ''; }
    else b.style.display = 'none';
  }

  // ── Fill ───────────────────────────────────────────────────────────────────

  async function doFill(sh) {
    const btn = sh.getElementById('fill-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Scanning…';
    setStatus(sh, 'd-ok', 'Scanning form fields…');

    const fields = scanFields();
    if (!fields.length) {
      setStatus(sh, 'd-warn', 'No fillable fields found');
      btn.disabled = false; btn.textContent = '⚡ Smart Fill This Form'; return;
    }

    jobContext = detectJobContext();
    btn.textContent = `⏳ Classifying ${fields.length} fields…`;

    try {
      const result = await apiCall('/smart-fill', 'POST', {
        domain: DOMAIN, url: location.href, fields, job_context: jobContext,
      });

      fills = result.fills || [];
      editedAnswers = {};

      // Immediately fill non-question, non-file fields
      let filled = 0;
      for (const f of fills) {
        if (!f.is_question && f.value && f.value !== '__resume__' && f.value !== '__file__') {
          if (fillField(f.field_id, f.value)) filled++;
        }
      }

      renderFills(sh, result, filled);
      sh.getElementById('learn-btn').style.display = 'flex';

      const instant = result.instant_hits || 0;
      const total   = result.total_fields || fields.length;
      const pct = total ? Math.round((instant / total) * 100) : 0;
      const bdgCls  = pct >= 70 ? 'b-instant' : 'b-ai';
      const bdgText = pct > 0 ? `${pct}% instant` : '🤖 AI';
      const qCount  = fills.filter(f => f.is_question && f.value).length;
      const note    = qCount ? ` + ${qCount} questions to review` : '';
      setStatus(sh, 'd-ok', `${filled} fields filled${note}`, bdgCls, bdgText);

    } catch (e) {
      const msg = (e.message||'').includes('connect') || (e.message||'').includes('fetch')
        ? 'Start the Job Agent server (localhost:8000)'
        : e.message;
      setStatus(sh, 'd-err', msg);
    }

    btn.disabled = false;
    btn.textContent = '↺ Re-fill Form';
  }

  // ── Render fills panel ─────────────────────────────────────────────────────

  function escHtml(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function renderFills(sh, result, filledCount) {
    const body = sh.getElementById('body');

    // Group by category
    const groups = {};
    for (const f of fills) {
      const cat = (f.canonical_type || 'unknown').split('.')[0];
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(f);
    }

    let html = '';

    if (jobContext?.title) {
      html += `<div class="job-ctx"><strong>Applying for:</strong> ${escHtml(jobContext.title)} at ${escHtml(jobContext.company)}</div>`;
    }

    // Show intelligence stats
    const instant = result.instant_hits || 0;
    const total   = result.total_fields || fills.length;
    if (instant > 0) {
      html += `<div class="intel">⚡ ${instant}/${total} fields recognized instantly from global learning</div>`;
    }

    const order = ['personal','social','compensation','experience','work_auth','compliance','question','file','unknown'];
    for (const cat of order) {
      const group = groups[cat];
      if (!group?.length) continue;
      const meta = CATEGORIES[cat] || CATEGORIES.unknown;

      if (cat === 'question') {
        html += renderQuestionGroup(group, meta);
      } else if (cat === 'file') {
        html += renderFileGroup(group, meta);
      } else {
        html += renderStdGroup(group, meta);
      }
    }

    if (!html) html = '<div class="empty">No fields classified on this page.</div>';
    body.innerHTML = html;

    // Wire question interactions
    for (const f of fills.filter(f => f.is_question)) {
      const editBtn = body.querySelector(`[data-edit="${f.field_id}"]`);
      const ta      = body.querySelector(`[data-ta="${f.field_id}"]`);
      const fillBtn = body.querySelector(`[data-qfill="${f.field_id}"]`);
      const now     = body.querySelector(`[data-qnow="${f.field_id}"]`);

      if (ta && f.value) ta.value = f.value;

      if (editBtn && ta) {
        editBtn.onclick = () => {
          const open = ta.classList.toggle('open');
          if (fillBtn) fillBtn.classList.toggle('open', open);
          editBtn.textContent = open ? 'Done' : 'Edit';
        };
      }
      if (fillBtn && ta) {
        fillBtn.onclick = () => {
          const val = ta.value;
          editedAnswers[f.field_id] = val;
          fillField(f.field_id, val);
          ta.classList.remove('open');
          fillBtn.classList.remove('open');
          if (editBtn) editBtn.textContent = 'Edit';
        };
      }
      // "Fill Now" button on the preview
      if (now) {
        now.classList.add('visible');
        now.onclick = () => {
          const val = (ta && ta.value) || f.value;
          fillField(f.field_id, val);
          now.textContent = '✓ Filled';
          now.style.opacity = '0.5';
        };
      }
    }
  }

  function renderStdGroup(group, meta) {
    const rows = group.map(f => {
      if (!f.value || f.value === '__file__' || f.value === '__resume__') return '';
      const sc = f.source==='learned'?'s-l':f.source==='static'?'s-s':'s-a';
      const sl = f.source==='learned'?'⚡ learned':f.source==='static'?'✓ known':'🤖 ai';
      const conf = Math.round((f.confidence||0)*100);
      const cc = conf>=90?'#10b981':conf>=70?'#f59e0b':'#ef4444';
      const lbl = (f.human_label||f.canonical_type).replace(/^[^.]+\./,'');
      const val = (f.value||'').length>36 ? f.value.slice(0,36)+'…' : f.value;
      return `<div class="frow">
        <div class="fl" title="${escHtml(f.human_label||'')}">
          ${escHtml(lbl)}
        </div>
        <div style="flex:1;min-width:0">
          <div class="fv" title="${escHtml(f.value)}">${escHtml(val)}</div>
          <div class="conf"><div class="cf" style="width:${conf}%;background:${cc}"></div></div>
        </div>
        <span class="fsrc ${sc}">${sl}</span>
      </div>`;
    }).join('');
    if (!rows.trim()) return '';
    return `<div class="cat">
      <div class="cat-hdr"><span>${meta.icon}</span><span style="color:${meta.color}">${meta.label}</span></div>
      ${rows}
    </div>`;
  }

  function renderQuestionGroup(group, meta) {
    const rows = group.map(f => {
      const sc = f.source==='learned'?'qs-l':f.source==='ai'?'qs-ai':'qs-c';
      const sl = f.source==='learned'?'⚡ learned':f.source==='ai'?'🤖 generated':'📋 cached';
      const lbl = (f.human_label||f.canonical_type).replace(/^question\./,'');
      const prev = (f.value||'No answer generated').slice(0,160);
      return `<div class="qrow">
        <div class="qtop">
          <div class="ql" title="${escHtml(f.human_label||'')}">${escHtml(lbl)}</div>
          <span class="qsrc ${sc}">${sl}</span>
          <button class="qedit" data-edit="${f.field_id}">Edit</button>
        </div>
        <div class="qprev">${escHtml(prev)}${(f.value||'').length>160?'…':''}</div>
        <textarea class="qta" data-ta="${f.field_id}" placeholder="Edit this answer before filling…"></textarea>
        <button class="qfill-btn" data-qfill="${f.field_id}">✓ Fill with edited answer</button>
        <button class="qfill-now" data-qnow="${f.field_id}">↓ Fill into form</button>
      </div>`;
    }).join('');
    if (!rows.trim()) return '';
    return `<div class="cat">
      <div class="cat-hdr">
        <span>${meta.icon}</span>
        <span style="color:${meta.color}">${meta.label}</span>
        <span style="font-size:9px;color:#475569;margin-left:4px">— review before filling</span>
      </div>
      ${rows}
    </div>`;
  }

  function renderFileGroup(group, meta) {
    const rows = group.map(f => {
      const lbl = f.canonical_type==='file.resume' ? 'Resume upload — attach manually' : 'File upload — attach manually';
      return `<div class="urow">
        <span style="font-size:14px">${meta.icon}</span>
        <span style="flex:1;font-size:11px;color:#94a3b8">${lbl}</span>
        <span class="ubdg">manual</span>
      </div>`;
    }).join('');
    return `<div class="cat">
      <div class="cat-hdr"><span>${meta.icon}</span><span style="color:${meta.color}">${meta.label}</span></div>
      ${rows}
    </div>`;
  }

  // ── Learn ──────────────────────────────────────────────────────────────────

  async function doLearn(sh) {
    logApplication(); // idempotent — no-op if already logged via form submit
    const btn = sh.getElementById('learn-btn');
    btn.disabled = true;
    btn.textContent = 'Saving…';

    const confirmed  = [];
    const corrections = [];

    for (const f of fills) {
      if (!f.fingerprint || f.canonical_type === 'unknown') continue;
      const el = document.querySelector(`[data-ja-id="${f.field_id}"]`);
      const currentVal = el?.value ?? f.value;
      const edited = editedAnswers[f.field_id];

      if (f.is_question && edited && edited !== f.value) {
        // They edited a question answer — still confirm the field TYPE classification was right
        confirmed.push({ fingerprint: f.fingerprint, canonical_type: f.canonical_type,
                         label: f.human_label || '', name: el?.name || '' });
      } else if (!f.is_question && f.value && currentVal && currentVal !== f.value
                 && f.source !== 'static') {
        // User changed a static field value — the mapping was wrong
        corrections.push({ fingerprint: f.fingerprint, correct_type: 'unknown',
                           label: f.human_label || '', name: el?.name || '' });
      } else if (f.value && f.confidence >= 0.6) {
        confirmed.push({ fingerprint: f.fingerprint, canonical_type: f.canonical_type,
                         label: f.human_label || '', name: el?.name || '' });
      }
    }

    try {
      const res = await apiCall('/learn-pattern', 'POST', {
        domain: DOMAIN, url: location.href,
        confirmed_fills: confirmed, corrections,
        fields_filled: fills.filter(f => f.value && !f.is_question).length,
        instant_hits:  fills.filter(f => f.source==='learned'||f.source==='static').length,
        ai_calls:      fills.some(f => f.source==='ai') ? 1 : 0,
      });
      setStatus(sh, 'd-ok',
        `${res.reinforced || 0} patterns saved globally — smarter next time`,
        'b-instant', `+${res.reinforced || 0} learned`
      );
      btn.textContent = '✓ Saved';
    } catch (e) {
      setStatus(sh, 'd-err', 'Save failed: ' + e.message);
      btn.disabled = false;
      btn.textContent = '✓ Submitted — Save What Worked';
    }
  }

  // ── Toggle ─────────────────────────────────────────────────────────────────

  function toggle(show) {
    panelVisible = show !== undefined ? show : !panelVisible;
    if (!shadow) shadow = createShadow();
    shadow.getElementById('panel').classList.toggle('hidden', !panelVisible);
  }

  // ── Messages ───────────────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener(msg => {
    if (msg.type === 'TOGGLE_PANEL') toggle();
    if (msg.type === 'OPEN_PANEL')   toggle(true);
  });

  // ── Auto-open ──────────────────────────────────────────────────────────────

  if (isJobPage()) {
    if (!shadow) shadow = createShadow();
    setupSubmitWatcher();
    setTimeout(() => {
      toggle(true);
      if (shadow) setStatus(shadow, 'd-ok', 'Job application page detected — ready to fill');
    }, 900);
  } else if (isJobListingPage()) {
    if (!shadow) shadow = createShadow();
    setupListingWatcher();
    setTimeout(injectFitRadar, 1200); // let the page finish rendering first
  }

})();
