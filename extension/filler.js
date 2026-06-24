/**
 * Job Agent Form Filler
 * Runs on all pages. When a job application form is detected:
 *  1. Scrapes fields and asks the backend to fill them
 *  2. Injects values and shows a floating status panel
 *  3. Watches for user edits and reports them back so the system learns
 */
(function () {
  "use strict";

  const API = "http://localhost:8000";
  const ATTR = "data-ja-filled";       // marks fields we filled
  const ATTR_FP = "data-ja-fp";        // fingerprint stored on element
  let _panel = null;
  let _learnTimer = null;
  let _pendingLearns = {};             // fingerprint → {label, value, corrected}
  let _filledCount = 0;
  let _jobContext = null;

  // ── Helpers ──────────────────────────────────────────────────────────────────

  function fingerprint(label, name, type) {
    const norm = (s) => s.toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
    const key = `${norm(label)}|${name.toLowerCase().replace(/[^a-z0-9_]/g, "")}|${type.toLowerCase()}`;
    // Simple 14-char hash matching Python's md5[:14]
    let h = 0;
    for (let i = 0; i < key.length; i++) {
      h = (Math.imul(31, h) + key.charCodeAt(i)) | 0;
    }
    return Math.abs(h).toString(16).padStart(8, "0").slice(0, 14);
  }

  function getLabel(el) {
    // Try explicit label element
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.innerText.trim();
    }
    // Closest wrapping label
    const wrap = el.closest("label");
    if (wrap) return wrap.innerText.replace(el.value || "", "").trim();
    // aria-label / aria-labelledby
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").trim();
    const lblId = el.getAttribute("aria-labelledby");
    if (lblId) {
      const lblEl = document.getElementById(lblId);
      if (lblEl) return lblEl.innerText.trim();
    }
    // Nearby label sibling or placeholder
    const prev = el.previousElementSibling;
    if (prev && ["LABEL", "SPAN", "DIV", "P"].includes(prev.tagName)) {
      const t = prev.innerText.trim();
      if (t.length > 0 && t.length < 80) return t;
    }
    return el.placeholder || el.name || "";
  }

  function getOptions(el) {
    if (el.tagName === "SELECT") {
      return Array.from(el.options).map((o) => o.text.trim()).filter(Boolean);
    }
    // Radio/checkbox group sharing the same name
    if (el.type === "radio" || el.type === "checkbox") {
      const group = document.querySelectorAll(`[name="${CSS.escape(el.name)}"]`);
      return Array.from(group).map((r) => {
        const lbl = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        return lbl ? lbl.innerText.trim() : r.value;
      }).filter(Boolean);
    }
    return [];
  }

  function scrapeFields() {
    const fields = [];
    const seen = new Set();
    const inputs = document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset])' +
      ', textarea, select, [contenteditable="true"]'
    );

    inputs.forEach((el, i) => {
      // Skip invisible or disabled
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      if (el.disabled || el.readOnly) return;

      const label = getLabel(el);
      const name  = el.name || el.getAttribute("data-field-name") || "";
      const type  = el.type || el.tagName.toLowerCase();
      const fp    = fingerprint(label, name, type);

      if (seen.has(fp) && type !== "radio") return;
      seen.add(fp);

      el.setAttribute(ATTR_FP, fp);

      fields.push({
        id:          fp,
        element_idx: i,
        label,
        name,
        type,
        placeholder: el.placeholder || "",
        options:     getOptions(el),
        required:    el.required || el.getAttribute("aria-required") === "true",
        current_value: el.value || el.innerText || "",
      });
    });

    return fields;
  }

  // ── Job context from page ────────────────────────────────────────────────────

  function detectJobContext() {
    const title = (
      document.querySelector("h1, [class*='job-title'], [class*='JobTitle'], [data-testid*='title']")
        ?.innerText?.trim() || ""
    ).slice(0, 120);
    const company = (
      document.querySelector("[class*='company'], [class*='Company'], [class*='employer'], [class*='Employer']")
        ?.innerText?.trim() || ""
    ).slice(0, 80);
    return { title, company, url: window.location.href };
  }

  // ── Fill DOM element ─────────────────────────────────────────────────────────

  function fillElement(el, value) {
    if (!value) return false;

    try {
      if (el.tagName === "SELECT") {
        const opts = Array.from(el.options);
        const match = opts.find(
          (o) => o.text.trim().toLowerCase() === value.toLowerCase() ||
                 o.value.toLowerCase() === value.toLowerCase()
        ) || opts.find((o) => o.text.trim().toLowerCase().includes(value.toLowerCase()));
        if (match) {
          el.value = match.value;
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return true;
        }
        return false;
      }

      if (el.type === "radio") {
        const group = document.querySelectorAll(`[name="${CSS.escape(el.name)}"]`);
        for (const r of group) {
          const lbl = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
          const text = (lbl?.innerText || r.value || "").toLowerCase();
          if (text.includes(value.toLowerCase()) || value.toLowerCase().includes(text)) {
            r.checked = true;
            r.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
          }
        }
        return false;
      }

      if (el.type === "checkbox") {
        const check = ["yes", "true", "1", "agree", "consent", "i agree"].includes(value.toLowerCase());
        if (check !== el.checked) {
          el.click();
        }
        return true;
      }

      if (el.getAttribute("contenteditable") === "true") {
        el.innerText = value;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      }

      // Standard text/email/tel/textarea
      // React/Vue track via nativeInputValueSetter
      const nativeSetter = Object.getOwnPropertyDescriptor(
        el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
        "value"
      )?.set;
      if (nativeSetter) {
        nativeSetter.call(el, value);
      } else {
        el.value = value;
      }
      el.dispatchEvent(new Event("input",  { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur",   { bubbles: true }));
      return true;
    } catch (e) {
      return false;
    }
  }

  // ── Apply fills from backend ─────────────────────────────────────────────────

  function applyFills(fills) {
    let filled = 0;
    fills.forEach((fill) => {
      if (!fill.value || fill.value.startsWith("__")) return;

      const el = document.querySelector(`[${ATTR_FP}="${CSS.escape(fill.field_id)}"]`);
      if (!el) return;

      if (fillElement(el, fill.value)) {
        el.setAttribute(ATTR, "true");
        el.style.boxShadow = "inset 0 0 0 2px rgba(0, 212, 100, 0.4)";
        el.title = `Job Agent: ${fill.canonical_type} (${fill.source})`;
        filled++;
        // Store the canonical type so we can learn corrections later
        el.setAttribute("data-ja-type", fill.canonical_type);
        el.setAttribute("data-ja-original", fill.value);
      }
    });
    return filled;
  }

  // ── Watch for user edits (learning) ─────────────────────────────────────────

  function watchForEdits() {
    document.addEventListener("change", onFieldChanged, true);
    document.addEventListener("blur",   onFieldChanged, true);
  }

  function onFieldChanged(e) {
    const el = e.target;
    if (!el || !el.getAttribute) return;
    const fp = el.getAttribute(ATTR_FP);
    if (!fp) return;

    const value   = (el.value || el.innerText || "").trim();
    const original = el.getAttribute("data-ja-original") || "";
    const wasFilled = el.getAttribute(ATTR) === "true";
    const corrected = wasFilled && value !== original;
    const label   = getLabel(el);

    if (!value) return;

    _pendingLearns[fp] = {
      fingerprint: fp,
      label,
      name: el.name || "",
      type: el.type || el.tagName.toLowerCase(),
      value,
      canonical_type: el.getAttribute("data-ja-type") || null,
      was_auto_filled: wasFilled,
      was_corrected: corrected,
      domain: window.location.hostname,
    };

    // Debounce — send after 2s of inactivity
    clearTimeout(_learnTimer);
    _learnTimer = setTimeout(sendLearns, 2000);

    // Visual feedback: turn ring yellow for corrections, green for new fills
    if (corrected) {
      el.style.boxShadow = "inset 0 0 0 2px rgba(251, 191, 36, 0.5)";
      updatePanel(`Learning correction for "${label}"…`);
    } else if (!wasFilled) {
      el.style.boxShadow = "inset 0 0 0 2px rgba(99, 102, 241, 0.35)";
    }
  }

  async function sendLearns() {
    const fields = Object.values(_pendingLearns);
    if (!fields.length) return;
    _pendingLearns = {};

    try {
      await fetch(`${API}/api/learn-field`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fields,
          url: window.location.href,
          domain: window.location.hostname,
          job_context: _jobContext,
        }),
      });
      const corrections = fields.filter((f) => f.was_corrected).length;
      const manual = fields.filter((f) => !f.was_auto_filled).length;
      if (corrections > 0) updatePanel(`Saved ${corrections} correction(s) — I'll remember this`);
      else if (manual > 0)  updatePanel(`Saved ${manual} new answer(s) to your profile`);
    } catch {
      // Silently fail — don't interrupt user
    }
  }

  // ── Floating panel ───────────────────────────────────────────────────────────

  function createPanel() {
    if (_panel) return;
    _panel = document.createElement("div");
    _panel.id = "ja-filler-panel";
    _panel.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
        <div style="width:7px;height:7px;border-radius:50%;background:#00d4ff;box-shadow:0 0 6px #00d4ff;flex-shrink:0;" id="ja-dot"></div>
        <span style="font-weight:800;font-size:11px;letter-spacing:.06em;color:#fff;">JOB AGENT</span>
        <span id="ja-close" style="margin-left:auto;cursor:pointer;color:rgba(255,255,255,.4);font-size:13px;line-height:1;">✕</span>
      </div>
      <div id="ja-status" style="font-size:11px;color:rgba(255,255,255,.65);line-height:1.45;"></div>
      <button id="ja-refill" style="margin-top:8px;width:100%;background:linear-gradient(135deg,#6366f1,#00d4ff);color:#fff;border:none;border-radius:5px;padding:5px 0;font-size:11px;font-weight:700;cursor:pointer;display:none;">⚡ Fill Again</button>
    `;
    Object.assign(_panel.style, {
      position: "fixed",
      bottom: "20px",
      right: "20px",
      zIndex: "2147483647",
      background: "rgba(13,21,40,0.96)",
      border: "1px solid rgba(0,212,255,.3)",
      borderRadius: "10px",
      padding: "12px 14px",
      width: "210px",
      boxShadow: "0 8px 32px rgba(0,0,0,.6),0 0 20px rgba(0,212,255,.08)",
      fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
    });

    document.body.appendChild(_panel);

    document.getElementById("ja-close").addEventListener("click", () => {
      _panel.style.display = "none";
    });
    document.getElementById("ja-refill").addEventListener("click", () => {
      runFiller(true);
    });
  }

  function updatePanel(msg, showRefill = false) {
    if (!_panel) return;
    const el = document.getElementById("ja-status");
    if (el) el.textContent = msg;
    if (showRefill) {
      const btn = document.getElementById("ja-refill");
      if (btn) btn.style.display = "block";
    }
    _panel.style.display = "block";
  }

  function setDotColor(color) {
    const dot = document.getElementById("ja-dot");
    if (dot) dot.style.background = color;
  }

  // ── Main filler pipeline ─────────────────────────────────────────────────────

  let _running = false;

  async function runFiller(forceRefill = false) {
    if (_running) return;
    _running = true;

    createPanel();
    updatePanel("Scanning form fields…");
    setDotColor("#00d4ff");

    const fields = scrapeFields();
    if (!fields.length) {
      updatePanel("No form fields detected on this page.");
      setDotColor("#555");
      _running = false;
      return;
    }

    // Only fill on pages that look like job applications (≥2 relevant fields)
    const appKeywords = ["name", "email", "phone", "resume", "cover", "experience", "salary", "linkedin", "address"];
    const looksLikeApp = fields.some((f) =>
      appKeywords.some((kw) => f.label.toLowerCase().includes(kw) || f.name.toLowerCase().includes(kw))
    );
    if (!looksLikeApp && !forceRefill) {
      _panel.style.display = "none";
      _running = false;
      return;
    }

    _jobContext = detectJobContext();
    updatePanel(`Found ${fields.length} fields — filling…`);

    try {
      const res = await fetch(`${API}/api/smart-fill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields, url: window.location.href, job_context: _jobContext }),
      });
      if (!res.ok) throw new Error(`Backend ${res.status}`);
      const data = await res.json();
      _filledCount = applyFills(data.fills || []);
      const meta = data.meta || {};
      const src = meta.instant_hits > 0 ? `${meta.instant_hits} from memory` : "AI";
      updatePanel(`Filled ${_filledCount}/${fields.length} fields (${src}). Edit anything — I'll learn.`, _filledCount < fields.length);
      setDotColor("#00ff88");
      watchForEdits();
    } catch (e) {
      updatePanel(`Backend offline — fill manually. Edits still saved.`);
      setDotColor("#f87171");
      watchForEdits(); // still watch so we learn from manual fills
    }

    _running = false;
  }

  // ── Entry point ──────────────────────────────────────────────────────────────

  // Listen for message from popup "FILL_PAGE"
  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === "FILL_PAGE") {
      runFiller(true);
      reply({ ok: true });
    }
    if (msg.type === "GET_FILL_STATUS") {
      reply({ filled: _filledCount, running: _running });
    }
  });

  // Auto-run on page load after a short delay for SPAs to render
  function tryAutoRun() {
    // Don't run on job board listing pages — only on application/form pages
    const skip = ["linkedin.com/jobs/", "indeed.com/viewjob", "glassdoor.com/job-listing",
                  "linkedin.com/feed", "google.com", "localhost"];
    if (skip.some((s) => window.location.href.includes(s))) return;
    setTimeout(() => runFiller(false), 1500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", tryAutoRun);
  } else {
    tryAutoRun();
  }

  // Re-run when URL changes (SPA navigation)
  let _lastUrl = location.href;
  const _observer = new MutationObserver(() => {
    if (location.href !== _lastUrl) {
      _lastUrl = location.href;
      _filledCount = 0;
      _pendingLearns = {};
      if (_panel) _panel.style.display = "none";
      tryAutoRun();
    }
  });
  _observer.observe(document.body, { childList: true, subtree: true });
})();
