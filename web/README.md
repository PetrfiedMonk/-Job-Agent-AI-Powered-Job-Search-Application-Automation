# Job Agent Web UI

A modern web interface for the Job Agent, built with **FastAPI** (backend) and **React** (frontend).

## Features

✨ **Dashboard** - View pipeline status in real-time
👤 **Profile** - Review your AI-synthesized profile from resume + Obsidian vault
📋 **Jobs** - Browse all found and applied jobs with scores
📊 **Real-time Logs** - Watch the agent run with live log streaming
⚙️ **Controls** - Start/stop searches with one click

## Architecture

```
web/
├── backend/
│   └── main.py          # FastAPI server
├── frontend/
│   ├── src/
│   │   ├── App.jsx      # Main React component
│   │   ├── App.css      # Styles
│   │   └── index.jsx    # React entry point
│   ├── public/
│   │   └── index.html   # HTML template
│   └── package.json     # React dependencies
└── requirements.txt     # Python dependencies
```

## Setup

### 1. Install Backend Dependencies

```bash
# From the web/ directory
pip install -r requirements.txt
```

### 2. Install Frontend Dependencies

```bash
cd frontend
npm install
```

### 3. Build the Frontend

```bash
cd frontend
npm run build
```

This creates a `build/` folder that the FastAPI server will serve.

## Running

### Option 1: Both Backend + Frontend (Production)

```bash
# From the web/ directory
python backend/main.py
```

Then open http://localhost:8000 in your browser.

### Option 2: Development Mode (Backend + Frontend Dev Server)

**Terminal 1 - Backend:**
```bash
python backend/main.py
```

**Terminal 2 - Frontend Dev Server:**
```bash
cd frontend
npm start
```

Then open http://localhost:3000 in your browser.

## API Endpoints

### Health Check
```
GET /api/health
```

### Profile
```
GET /api/profile
```
Returns the user's AI-synthesized profile.

### Status
```
GET /api/status
```
Returns current pipeline status (running, jobs found, scored, applied).

### Jobs
```
GET /api/jobs?status=found&limit=50
```
Returns job results from database.

### Start Search
```
POST /api/start-search
```
Starts a job search in the background.

### Stop Pipeline
```
POST /api/stop-pipeline
```
Stops the running pipeline.

### WebSocket Logs
```
WS /ws/logs
```
Real-time log stream during pipeline execution.

## Environment

Make sure your `.env` or environment variables are set:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

The backend reads from `config.yaml` and uses the same configuration as the CLI version.

## Performance

- **Frontend**: React 18, builds to ~50KB gzipped
- **Backend**: FastAPI with async/WebSocket support
- **Database**: SQLite queries for job results
- **Real-time**: WebSocket streaming for logs

## Troubleshooting

### Port Already in Use
If port 8000 is already in use:
```bash
python backend/main.py --port 8001
```

### CORS Errors
The backend is configured to allow all origins in development. For production, update:
```python
# web/backend/main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=["yourdomain.com"],  # Change this
    ...
)
```

### Frontend Build Issues
```bash
cd frontend
rm -rf node_modules build
npm install
npm run build
```

## File Structure

```
web/
├── backend/
│   ├── __init__.py
│   └── main.py           # FastAPI app with all endpoints
├── frontend/
│   ├── public/
│   │   └── index.html    # HTML template
│   ├── src/
│   │   ├── App.css       # All styles (Tailwind-inspired)
│   │   ├── App.jsx       # React component
│   │   └── index.jsx     # Entry point
│   ├── package.json      # Dependencies
│   └── .gitignore        # Ignore node_modules, build
├── requirements.txt      # Python packages
└── README.md             # This file
```

## Next Steps

1. ✅ Run the backend
2. ✅ Build the frontend
3. ✅ Open http://localhost:8000
4. ✅ Click "Start Search"
5. ✅ Watch real-time logs and results

Happy job hunting! 🚀
