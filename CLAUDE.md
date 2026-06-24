# CLAUDE.md — Job Agent Project Index

**Read this file first every session.** It is the map. Use it to jump directly to any file, function, or endpoint instead of doing exploratory reads.

---

## System Architecture

```
User ──► Web UI (index.html, localhost:8000)
              │
         FastAPI backend (web/backend/main.py)
              │
     ┌────────┼──────────────┐
     │        │              │
  SQLite   Anthropic      Playwright
  (2 DBs)   Claude API    browser
     │
     ├── job_agent.db         (jobs, applications, run_scores)
     └── field_semantics.db   (field_semantics, answer_cache, form_submissions)

Chrome Extension (web/extension/)
     ├── background.js   service worker — relays API calls to http://localhost:8000/api
     └── content.js      two modes:
                          • Fit Radar  — job listing pages: inject score badges
                          • Smart Fill — application pages: fill form fields
```

The frontend (`index.html`) is a **single file** — all HTML, CSS, and JS together. No build step. Edit it directly.

The backend (`main.py`) is a **single file** FastAPI app, ~3400 lines. SQLite only, no external DB.

---

## File Map

| File | Purpose | Size |
|------|---------|------|
| `web/backend/main.py` | FastAPI REST API + WebSocket log stream | ~3400 lines |
| `web/frontend/index.html` | All-in-one web UI | ~4600 lines |
| `web/extension/background.js` | Chrome extension service worker (API relay) | ~100 lines |
| `web/extension/content.js` | Chrome extension content script (Fit Radar + Smart Fill + auto-log) | ~900 lines |
| `web/extension/manifest.json` | MV3 manifest |  |
| `web/extension/popup.html/js` | Extension toolbar popup |  |
| `job_agent/config.py` | Config dataclasses + YAML loader |  |
| `job_agent/orchestrator.py` | Top-level pipeline coordinator |  |
| `job_agent/models.py` | JobPosting, UserProfile, Application dataclasses |  |
| `job_agent/ai/profile_builder.py` | Claude: vault + resume → UserProfile |  |
| `job_agent/ai/job_scorer.py` | Claude: batch job scoring + country filter |  |
| `job_agent/ai/resume_tailor.py` | Claude: per-job resume tailoring |  |
| `job_agent/ai/form_filler.py` | Smart Fill: classify + answer form fields |  |
| `job_agent/ai/vault_recommender.py` | Obsidian vault note recommendations per job |  |
| `job_agent/search/job_searcher.py` | Multi-platform scraping via python-jobspy |  |
| `job_agent/automation/application_agent.py` | Playwright: navigate + fill + submit |  |
| `job_agent/automation/ats_handlers.py` | Platform-specific ATS quirk handling |  |
| `job_agent/db/tracker.py` | SQLite: jobs, applications, run_scores tables |  |
| `job_agent/db/field_semantics.py` | Global form field learning + Answer Playbook |  |
| `job_agent/db/run_log.py` | Markdown run log writer |  |
| `job_agent/db/improvement_tracker.py` | Self-improvement log |  |
| `config.example.yaml` | Template — copy to config.yaml and fill in |  |
| `config.yaml` | Live config (gitignored — never committed) |  |
| `setup.bat` / `setup.sh` | Windows / Mac+Linux installer |  |
| `start_job_agent.bat` / `.sh` | Launchers |  |

---

## Backend Endpoint Index (`web/backend/main.py`)

All routes are under `/api/` prefix. Line numbers are approximate — grep `@app.` to verify after edits.

### Health / Status
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 163 | GET | `/api/health` | |
| 172 | GET | `/api/profile` | AI-synthesized profile |
| 220 | GET | `/api/status` | Pipeline running state |

### Jobs
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 256 | GET | `/api/jobs` | All jobs with scores |
| 311 | POST | `/api/jobs/score-preview` | Batch keyword scoring, no AI — used by Fit Radar |
| 384 | POST | `/api/jobs/add-to-pipeline` | Save job discovered by extension |
| 626 | GET | `/api/jobs/{id}/brief` | Match brief for a job |
| 782 | GET | `/api/jobs/{id}/resume` | Fetch tailored resume |
| 849 | POST | `/api/jobs/{id}/generate-resume` | Trigger Claude resume tailoring |
| 1111 | POST | `/api/auto-apply` | Start batch auto-apply |
| 1125 | POST | `/api/jobs/{id}/apply` | Auto-apply single job |
| 1282 | POST | `/api/jobs/{id}/queue` | Add job to attack queue |
| ~1295 | DELETE | `/api/jobs/{id}/queue` | Remove from queue |
| ~1305 | GET | `/api/queue` | List all queued jobs |
| ~1315 | DELETE | `/api/queue` | Clear entire queue |
| ~1325 | POST | `/api/queue/launch` | Launch queue attack (background) |
| ~1342 | GET | `/api/needs-manual` | Jobs needing manual apply |
| 1294 | POST | `/api/jobs/{id}/mark-applied-manually` | Mark as manually applied |
| 1312 | POST | `/api/jobs/{id}/dismiss` | Dismiss a job |
| 2258 | POST | `/api/track-job` | Add job to tracker |

### Pipeline
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 432 | POST | `/api/start-search` | Start full pipeline run |
| 451 | POST | `/api/stop-pipeline` | Stop running pipeline |
| 3055 | POST | `/api/scan-jobs` | Scan-only (no apply) |
| 3124 | GET | `/api/run-log` | Run history |
| 3138 | DELETE | `/api/run-log` | Clear run log |
| 3154 | GET | `/api/fix-proposals` | Improvement proposals |
| ~3373 | GET | `/api/next-action` | Next action card data |

### Smart Fill / Field Learning
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 1363 | POST | `/api/smart-fill` | Classify + fill form fields |
| 1400 | POST | `/api/learn-pattern` | Reinforce field type mappings |
| 1445 | POST | `/api/learn-field` | Learn individual field |
| 1584 | GET | `/api/known-sites` | Sites with learned patterns |
| 1592 | GET | `/api/field-intelligence` | Field type stats |
| 1849 | GET | `/api/field-memory` | List saved field memories |
| 1854 | POST | `/api/field-memory` | Add field memory |
| 1881 | DELETE | `/api/field-memory` | Delete field memory |

### Answer Playbook
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 1602 | GET | `/api/playbook` | List all user-saved answers |
| 1618 | POST | `/api/playbook` | Save user-edited answer (`source='user'`) |
| 1662 | DELETE | `/api/playbook/{id}` | Delete playbook entry |

### Content Generation
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 483 | POST | `/api/cover-letter/{id}` | Generate cover letter |
| 559 | POST | `/api/outreach/{id}` | Generate outreach kit |
| 4385 | POST (fn) | (internal) | Interview prep generator |

### Config / Settings
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 1711 | GET | `/api/config` | Read config |
| 1723 | POST | `/api/config` | Write config (persists to YAML) |
| 1964 | POST | `/api/open-browser` | Open browser to URL |
| 2095 | GET | `/api/login-status` | Platform login states |
| 2333 | POST | `/api/open-login/{platform}` | Launch platform login |

### Performance / Gamification
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 1942 | GET | `/api/performance` | Run performance metrics |
| 719 | GET | `/api/vault/recommendations` | Vault note recs per job |

### Extension / Automation
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 1833 | POST | `/api/captcha-solved` | Extension signals CAPTCHA solved |
| 2276 | POST | `/api/extension/log-application` | Log submitted application from extension |

### WebSocket / Static
| Line | Method | Path | Notes |
|------|--------|------|-------|
| 2350 | WS | `/ws/logs` | Live pipeline log stream |
| 2363 | GET | `/` | Serve index.html |

---

## Frontend Function Index (`web/frontend/index.html`)

### Core Utilities
| Line | Function | Notes |
|------|----------|-------|
| 1751 | `pMsg(type, ctx)` | Personalized loading messages using `_profile` cache |
| 1809 | `pEmpty(type)` | Profile-aware empty state HTML |
| 1840 | `_onProfileCacheLoaded()` | Fires when profile loads; wires up pMsg/pEmpty |
| 1861 | `getRarity(score)` | Score → COMMON/UNCOMMON/RARE/EPIC/LEGENDARY |
| 1926 | XP/Level constants | `XP_PER_LEVEL`, `LEVEL_NAMES[]` |
| 1947 | `addXP(amount, reason)` | Award XP + show toast |
| 2133 | `toast(msg, type)` | Show notification toast |
| 2142 | `showTab(name)` | Switch main tabs |
| 2160 | `setFilter(f)` | Set job list filter |

### Job Actions
| Line | Function | Notes |
|------|----------|-------|
| 2185 | `markJobApplied(id, btn)` | Mark applied + reload next action |
| 2200 | `dismissJob(id, btn)` | Dismiss job |
| 2213 | `renderJobs(jobs)` | Render job cards list |
| 2281 | `renderTracker(apps)` | Render application tracker |
| 2330 | `applyOneJob(id, btn)` | Trigger auto-apply for one job |
| 2394 | `openIntel(jobId, btn)` | Open outreach intel panel |

### Profile / Setup
| Line | Function | Notes |
|------|----------|-------|
| 2469 | `loadProfile()` | Fetch + display profile |
| 2627 | `rescanProfile()` | Trigger deep vault rescan |
| 4458 | `showWizardStep(step)` | First-run wizard |
| 4491 | `completeWizard()` | Finish wizard + save config |
| 4510 | `checkFirstRun()` | Auto-show wizard if unconfigured |

### Pipeline Controls
| Line | Function | Notes |
|------|----------|-------|
| 2645 | `startAutoApply()` | Launch full pipeline |
| 2680 | `startScanJobs()` | Scan only |
| 2697 | `loadRunLog()` | Load run history |

### Manual Apply Kit
| Line | Function | Notes |
|------|----------|-------|
| 2757 | `loadNeedsManual()` | Load jobs needing manual apply |
| 2801 | `openManualKit(jobId)` | Open manual apply kit drawer |
| 2855 | `kitBuildResume(jobId, btn)` | Generate tailored resume in kit |

### Dashboard Cards
| Line | Function | Notes |
|------|----------|-------|
| 4310 | `loadAutopilot()` | Autopilot toggle state |
| 4315 | `loadMomentum()` | Momentum score card |
| 4334 | `loadNextAction()` | Next action card — hides card when no action |

### Settings
| Line | Function | Notes |
|------|----------|-------|
| 3500 | `loadConfig()` | Load config into settings form |
| 3553 | `_updateSettingsGreeting(s, p)` | Settings page header |
| 3573 | `checkProfileSetup(p)` | Warn if profile incomplete |
| 3593 | `loadLoginStatus()` | Platform login badges |
| 3625 | `openLogin(platform)` | Launch platform login |
| 3659 | `saveApiKey()` | Save Anthropic key |
| 3671 | `saveConfig()` | Save full config form |
| 3700 | `updateLiveScore()` | Live run score display |
| 3756 | `scoreColor(s)` / `barClass(s)` | Score → CSS class |
| 3759 | `renderScoreData(d)` | Render run performance |
| 4355 | `showSettingsSection(name, btn)` | Settings sub-nav |

### Match Brief / Vault
| Line | Function | Notes |
|------|----------|-------|
| 3842 | `showBriefState(state)` | Brief panel state |
| 3849 | `openMatchBrief(jobId, btn)` | Open match brief co-pilot |
| 3874 | `_renderVaultRecsHTML(recs, ...)` | Vault recs HTML |
| 3904 | `loadVaultRecs()` | Load vault recommendations |
| 4020 | `getResume(jobId, btn)` | Fetch + display tailored resume |
| 4031 | `renderBrief(d)` | Render full match brief |
| 4095 | `briefAutoApply()` | Auto-apply from brief |
| 4132 | `briefManualApply()` | Manual apply from brief |

### Teach / Field Memory
| Line | Function | Notes |
|------|----------|-------|
| 4216 | `showAddMemoryForm()` | Show add memory form |
| 4220 | `loadMemories()` | Load all field memories |
| 4260 | `saveMemory()` | Save new field memory |
| 4277 | `deleteMemory(id)` | Delete field memory |

### UI / Modal
| Line | Function | Notes |
|------|----------|-------|
| 4396 | `showModal(title, sub, body)` | Generic modal |
| 4417 | `openCmdPalette()` | Open command palette |
| 4430 | `cmdSearch(val)` | Filter command palette |
| 3454 | `updateKwCount()` | Keyword count indicator |
| 3468 | `_updateKwSuggestions()` | Keyword suggestions |
| 3494 | `openRoleScout()` | Open role scout panel |

---

## Database Schema

### `job_agent.db` (managed by `job_agent/db/tracker.py`)

**`jobs`** — all discovered job postings
- `id`, `title`, `company`, `location`, `url`, `description`
- `score` (0–100 fit), `salary_score` (0–100), `combined_score`
- `status` (`new` / `applied` / `manual_apply` / `dismissed`)
- `applied_at`, `created_at`, `platform`

**`applications`** — submitted applications
- `id`, `job_id` (FK), `resume_path`, `cover_letter`
- `applied_at`, `method` (`auto` / `manual`)

**`run_scores`** — per-run performance metrics
- `id`, `run_date`, `jobs_found`, `jobs_applied`, `automation_rate`
- `performance_grade`, `score_breakdown` (JSON)

### `field_semantics.db` (managed by `job_agent/db/field_semantics.py`)

**`field_semantics`** — learned field type mappings
- `id`, `label_text`, `field_type`, `confidence`, `site_domain`
- `created_at`, `updated_at`

**`answer_cache`** — cached question answers
- `id`, `canonical_type`, `label`, `answer`, `company`, `job_title`
- `source` (`'ai'` default or `'user'`) — **user entries take priority in reads**
- UNIQUE on `(canonical_type, company)`
- User entries are protected: AI cannot overwrite them on `ON CONFLICT`

**`form_submissions`** — auto-submission log from extension
- `id`, `job_url`, `job_id`, `submitted_at`, `fields_filled`

---

## Extension Architecture (`web/extension/`)

### background.js — Service Worker
- Relays all API calls from content script → `http://localhost:8000/api`
- `const API = 'http://localhost:8000/api'` — change port here if needed
- `apiCall(endpoint, opts)` → wraps fetch, returns JSON
- No DOM access; message passing only via `chrome.runtime.onMessage`

### content.js — Content Script
Runs on every page. Two modes detected at runtime:

**Fit Radar mode** (job listing pages: LinkedIn Jobs, Indeed, Glassdoor, ZipRecruiter)
- `isJobListingPage()` — URL pattern detection
- `extractJobCards()` — platform-specific card selectors
- `injectBadge(card, score)` — adds badge directly to host page DOM (not shadow DOM)
- `injectFitRadar()` — calls `/api/jobs/score-preview`, badges all cards
- `setupListingWatcher()` — MutationObserver, debounced 700ms, handles SPA nav + infinite scroll
- `[data-ja-radar]` attribute — guards against double-badge injection

**Fit scoring** (no AI, pure keyword match):
- Title match: 2× weight → up to 55 pts
- Description match: up to 25 pts
- Skill match: up to 20 pts
- Floor at 25 if zero keyword matches

**Smart Fill mode** (job application forms)
- Shadow DOM panel injected into page
- `setupSubmitWatcher()` — listens for `submit` events + submit-button clicks (capture phase)
- `logApplication()` — idempotent via `_submitLogged` flag, calls `/api/extension/log-application`
- `doLearn()` — calls `logApplication()` as belt-and-suspenders when ✓ Submitted is clicked

---

## Key Patterns

### Add a new backend endpoint
```python
@app.post("/api/my-endpoint")
async def my_endpoint(body: MyModel):
    # do stuff
    return {"ok": True}
```
Then add a row to the **API Reference** table in `README.md`.

### Add a new DB column (additive migration)
In the relevant `_init_schema()` method:
```python
try:
    conn.execute("ALTER TABLE my_table ADD COLUMN new_col TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass  # column already exists
```

### Add a new frontend tab
1. Add tab button in HTML with `onclick="showTab('my-tab')"`
2. Add `<div id="tab-my-tab" class="tab-content">` section
3. Add `case 'my-tab':` in `showTab()` at line 2142

### Extension calls backend
```javascript
// In content.js — use background relay
chrome.runtime.sendMessage({type:'API', endpoint:'/my-endpoint', method:'POST', body:{...}}, r => {
  if(r.ok){ /* handle */ }
});
```

### Profile-aware loading message
```javascript
const msg = pMsg('scanning', {company: 'Stripe'});  // uses _profile global
```

### User playbook answer priority
When reading answers, `get_cached_answer()` always orders `source='user'` first. AI fills call `cache_answer()` which has an `ON CONFLICT` guard that skips the update if `source='user'`.

---

## Platform Enum Values
`indeed`, `linkedin`, `company`, `ziprecruiter`, `glassdoor`

## WebSocket Log Stream
`ws://localhost:8000/ws/logs` — `ConnectionManager` at ~line 155 in `main.py`. Every `await manager.broadcast(msg)` call appears in the live log panel. Message format: plain string.

## Config Safety
- `config.yaml` is in `.gitignore` — **never committed**
- `config.example.yaml` is the safe template with placeholder values
- `debug.log` is also gitignored

## GitHub Remote
`https://github.com/PetrfiedMonk/-Job-Agent-AI-Powered-Job-Search-Application-Automation.git` — branch `main`

---

## Quick Reference: "Where is X?"

| What | Where |
|------|-------|
| Add/change a REST endpoint | `web/backend/main.py` — find via `@app.get/post/delete` |
| Change how jobs are scored | `job_agent/ai/job_scorer.py` |
| Change resume tailoring prompt | `job_agent/ai/resume_tailor.py` |
| Change Smart Fill field classification | `job_agent/ai/form_filler.py` + `job_agent/db/field_semantics.py` |
| Change Fit Radar scoring weights | `web/extension/content.js` → `injectFitRadar()` function |
| Change dashboard cards / XP / gamification | `web/frontend/index.html` lines 1861–1947 |
| Add a new settings field | `web/frontend/index.html` → settings HTML + `saveConfig()` + `web/backend/main.py` → `POST /api/config` + `job_agent/config.py` dataclass |
| Change answer playbook logic | `job_agent/db/field_semantics.py` → `save_to_playbook()`, `list_playbook()`, `get_cached_answer()` |
| Change Next Action card logic | `web/backend/main.py` ~line 3373 `get_next_action()` + `web/frontend/index.html` line 4334 `loadNextAction()` |
| Change auto-submission logging | `web/extension/content.js` → `logApplication()`, `setupSubmitWatcher()` + `web/backend/main.py` line 2276 |
| Change queue attack logic | `web/backend/main.py` → `run_queue_attack()` + `job_agent/db/tracker.py` → `set_queued/get_queued_jobs/clear_queue` |
| Change queue dock / overlay UI | `web/frontend/index.html` → CSS classes `qdock-*` / `qov-*`, HTML `#queue-dock` / `#queue-overlay`, JS `toggleQueue/launchQueueAttack/handleQueueComplete` |
| Change pipeline flow | `job_agent/orchestrator.py` |
| Change platform scraping | `job_agent/search/job_searcher.py` |
| Change DOCX resume output | `job_agent/builders/resume_builder.py` |
| Change Obsidian vault writes | `job_agent/db/run_log.py` + `job_agent/ai/vault_recommender.py` |
| Change installer steps | `setup.bat` (Windows) + `setup.sh` (Mac/Linux) |
