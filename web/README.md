# Job Agent Web UI

A modern web interface for the Job Agent, built with **FastAPI** backend and **vanilla HTML/CSS/JavaScript** frontend (no npm required!).

## Features

✨ **Dashboard** - View pipeline status in real-time
👤 **Profile** - Review your AI-synthesized profile from resume + Obsidian vault
📋 **Jobs** - Browse all found and applied jobs with scores
📊 **Real-time Logs** - Watch the agent run with live log streaming
⚙️ **Controls** - Start/stop searches with one click

## Quick Start

### 1. Install Dependencies

```bash
pip install -r web/requirements.txt
```

### 2. Run the Server

```bash
python web/backend/main.py
```

### 3. Open Browser

Visit **http://localhost:8000** 🌐

That's it! The frontend is a single HTML file with no build steps needed.

---

## Architecture

```
web/
├── backend/
│   └── main.py          # FastAPI server with REST API + WebSocket
├── frontend/
│   ├── index.html       # All-in-one UI (HTML + CSS + JS)
│   └── package.json     # (Optional - for future React migration)
└── requirements.txt     # Python dependencies
```

## Features in Detail

### Dashboard
- Real-time pipeline status
- 4 key metrics (Jobs Found, Scored, Applied)
- Start/Stop controls
- Current step indicator

### Profile
- Your name, email, location
- LinkedIn profile
- Summary from resume + Obsidian vault
- Top skills from AI synthesis

### Jobs
- Browse all discovered jobs
- Fit score visualization
- Salary information
- Direct links to job postings
- Status badges (found, scored, applied, rejected)

### Logs
- Real-time log streaming via WebSocket
- Color-coded messages (info, success, error)
- Auto-scrolling log viewer
- Complete pipeline activity history

---

## API Reference

### REST Endpoints

```
GET  /api/health                  # Health check
GET  /api/profile                 # Get AI-synthesized profile
GET  /api/status                  # Get pipeline status
GET  /api/jobs?status=X&limit=50  # Get jobs from database
POST /api/start-search            # Start job search
POST /api/stop-pipeline           # Stop running pipeline
```

### WebSocket

```
WS /ws/logs                        # Real-time log streaming
```

---

## Environment Setup

Make sure your environment variables are set:

```bash
# Set your Anthropic API key
$env:ANTHROPIC_API_KEY = "sk-ant-YOUR_KEY_HERE"

# Then run the server
python web/backend/main.py
```

The backend reads from `config.yaml` in the parent directory.

---

## How It Works

1. **Frontend** sends requests to FastAPI REST API
2. **Backend** connects to Job Agent core (same as CLI)
3. **WebSocket** streams real-time logs during execution
4. **Database** is shared with CLI (output/applications.db)

---

## Troubleshooting

### Port Already in Use
```bash
python web/backend/main.py --port 8001
```

### Backend Not Responding
- Verify the backend is running: `python web/backend/main.py`
- Check that port 8000 is not blocked
- Ensure your API key is set: `echo $env:ANTHROPIC_API_KEY`

### WebSocket Connection Failed
- This is normal if the backend just started
- The frontend will auto-reconnect
- Check browser console for details

---

## Performance

- **Frontend**: Single HTML file, ~50KB
- **Backend**: FastAPI with async/WebSocket support
- **Database**: SQLite queries for instant results
- **Real-time**: WebSocket streaming with <100ms latency

---

## API Documentation

Once running, visit:

```
http://localhost:8000/docs
```

This opens the interactive Swagger UI with all API endpoints documented.

---

## Next Steps

1. ✅ `pip install -r web/requirements.txt`
2. ✅ `python web/backend/main.py`
3. ✅ Open http://localhost:8000
4. ✅ Click "Start Search"
5. ✅ Watch real-time logs and results

Happy job hunting! 🚀
