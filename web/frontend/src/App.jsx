import React, { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import './App.css';

function App() {
  const [status, setStatus] = useState({
    is_running: false,
    current_step: null,
    jobs_found: 0,
    jobs_scored: 0,
    jobs_applied: 0,
  });
  
  const [profile, setProfile] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [logs, setLogs] = useState([]);
  const [activeTab, setActiveTab] = useState('dashboard');
  const [loading, setLoading] = useState(true);
  const logsEndRef = useRef(null);
  
  const API_URL = 'http://localhost:8000/api';
  const WS_URL = 'ws://localhost:8000/ws/logs';
  
  // Scroll logs to bottom
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);
  
  // Initial load
  useEffect(() => {
    loadProfile();
    loadStatus();
    loadJobs();
    
    const statusInterval = setInterval(loadStatus, 2000);
    const jobsInterval = setInterval(loadJobs, 5000);
    
    return () => {
      clearInterval(statusInterval);
      clearInterval(jobsInterval);
    };
  }, []);
  
  // WebSocket for logs
  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'log') {
          setLogs(prev => [...prev, data]);
        } else if (data.type === 'complete') {
          setLogs(prev => [...prev, { ...data, type: 'success' }]);
        } else if (data.type === 'error') {
          setLogs(prev => [...prev, { ...data, type: 'error' }]);
        }
      } catch (e) {
        console.error('Failed to parse log:', e);
      }
    };
    
    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };
    
    return () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.close();
      }
    };
  }, []);
  
  const loadProfile = async () => {
    try {
      const response = await axios.get(`${API_URL}/profile`);
      setProfile(response.data);
    } catch (error) {
      console.error('Failed to load profile:', error);
    }
  };
  
  const loadStatus = async () => {
    try {
      const response = await axios.get(`${API_URL}/status`);
      setStatus(response.data);
      setLoading(false);
    } catch (error) {
      console.error('Failed to load status:', error);
    }
  };
  
  const loadJobs = async () => {
    try {
      const response = await axios.get(`${API_URL}/jobs?limit=100`);
      setJobs(response.data);
    } catch (error) {
      console.error('Failed to load jobs:', error);
    }
  };
  
  const startSearch = async () => {
    try {
      await axios.post(`${API_URL}/start-search`);
      setLogs([]);
    } catch (error) {
      if (error.response?.status === 409) {
        alert('Pipeline already running!');
      } else {
        alert('Failed to start search: ' + error.message);
      }
    }
  };
  
  const stopPipeline = async () => {
    try {
      await axios.post(`${API_URL}/stop-pipeline`);
    } catch (error) {
      alert('Failed to stop pipeline: ' + error.message);
    }
  };
  
  if (loading && !profile) {
    return <div className="app loading">Loading Job Agent...</div>;
  }
  
  return (
    <div className="app">
      <header className="header">
        <h1>🤖 Job Agent</h1>
        <p>AI-Powered Job Search & Application Automation</p>
      </header>
      
      <nav className="nav-tabs">
        <button
          className={`tab ${activeTab === 'dashboard' ? 'active' : ''}`}
          onClick={() => setActiveTab('dashboard')}
        >
          Dashboard
        </button>
        <button
          className={`tab ${activeTab === 'profile' ? 'active' : ''}`}
          onClick={() => setActiveTab('profile')}
        >
          Profile
        </button>
        <button
          className={`tab ${activeTab === 'jobs' ? 'active' : ''}`}
          onClick={() => setActiveTab('jobs')}
        >
          Jobs ({jobs.length})
        </button>
        <button
          className={`tab ${activeTab === 'logs' ? 'active' : ''}`}
          onClick={() => setActiveTab('logs')}
        >
          Logs
        </button>
      </nav>
      
      <main className="main">
        {activeTab === 'dashboard' && (
          <div className="tab-content">
            <h2>Pipeline Status</h2>
            
            <div className="status-cards">
              <div className={`card status-card ${status.is_running ? 'running' : ''}`}>
                <div className="card-label">Status</div>
                <div className="card-value">
                  {status.is_running ? (
                    <>
                      <span className="pulse"></span> Running
                    </>
                  ) : (
                    'Idle'
                  )}
                </div>
              </div>
              
              <div className="card">
                <div className="card-label">Jobs Found</div>
                <div className="card-value">{status.jobs_found}</div>
              </div>
              
              <div className="card">
                <div className="card-label">Jobs Scored</div>
                <div className="card-value">{status.jobs_scored}</div>
              </div>
              
              <div className="card">
                <div className="card-label">Jobs Applied</div>
                <div className="card-value">{status.jobs_applied}</div>
              </div>
            </div>
            
            {status.current_step && (
              <div className="current-step">
                <strong>Current Step:</strong> {status.current_step}
              </div>
            )}
            
            <div className="controls">
              <button
                className="btn btn-primary"
                onClick={startSearch}
                disabled={status.is_running}
              >
                🔍 Start Search
              </button>
              
              <button
                className="btn btn-danger"
                onClick={stopPipeline}
                disabled={!status.is_running}
              >
                ⏹️ Stop Pipeline
              </button>
            </div>
          </div>
        )}
        
        {activeTab === 'profile' && profile && (
          <div className="tab-content">
            <h2>Your AI Profile</h2>
            
            <div className="profile-card">
              <h3>{profile.name}</h3>
              <p><strong>Email:</strong> {profile.email}</p>
              <p><strong>Phone:</strong> {profile.phone}</p>
              <p><strong>Location:</strong> {profile.location}</p>
              {profile.linkedin_url && (
                <p><strong>LinkedIn:</strong> <a href={profile.linkedin_url} target="_blank" rel="noopener noreferrer">View Profile</a></p>
              )}
              
              <div className="section">
                <h4>Summary</h4>
                <p>{profile.summary}</p>
              </div>
              
              {profile.unique_value_props && profile.unique_value_props.length > 0 && (
                <div className="section">
                  <h4>Unique Value Props</h4>
                  <ul>
                    {profile.unique_value_props.map((prop, i) => (
                      <li key={i}>{prop}</li>
                    ))}
                  </ul>
                </div>
              )}
              
              {profile.skills && profile.skills.length > 0 && (
                <div className="section">
                  <h4>Top Skills</h4>
                  <div className="skills-list">
                    {profile.skills.map((skill, i) => (
                      <span key={i} className="skill-tag">{skill}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
        
        {activeTab === 'jobs' && (
          <div className="tab-content">
            <h2>Job Results</h2>
            
            {jobs.length === 0 ? (
              <p className="empty">No jobs yet. Start a search to find opportunities!</p>
            ) : (
              <div className="jobs-list">
                {jobs.map((job) => (
                  <div key={job.id} className="job-card">
                    <div className="job-header">
                      <h3>{job.title}</h3>
                      <span className={`status-badge status-${job.status}`}>
                        {job.status}
                      </span>
                    </div>
                    
                    <p className="company">{job.company}</p>
                    <p className="location">📍 {job.location}</p>
                    
                    {job.salary_min && job.salary_max && (
                      <p className="salary">
                        💰 ${job.salary_min.toLocaleString()} - ${job.salary_max.toLocaleString()}
                      </p>
                    )}
                    
                    <div className="scores">
                      <div className="score">
                        <span>Fit Score</span>
                        <div className="score-bar">
                          <div className="score-fill" style={{width: `${job.fit_score}%`}}></div>
                        </div>
                        <span>{job.fit_score.toFixed(1)}%</span>
                      </div>
                      
                      <div className="score">
                        <span>Combined</span>
                        <div className="score-bar">
                          <div className="score-fill" style={{width: `${job.combined_score}%`}}></div>
                        </div>
                        <span>{job.combined_score.toFixed(1)}%</span>
                      </div>
                    </div>
                    
                    <a href={job.url} target="_blank" rel="noopener noreferrer" className="btn btn-secondary">
                      View Job
                    </a>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        
        {activeTab === 'logs' && (
          <div className="tab-content">
            <h2>Real-time Logs</h2>
            
            <div className="logs-container">
              {logs.length === 0 ? (
                <p className="empty">No logs yet. Start a search to see activity!</p>
              ) : (
                logs.map((log, i) => (
                  <div key={i} className={`log-line log-${log.type}`}>
                    <span className="timestamp">{new Date(log.timestamp).toLocaleTimeString()}</span>
                    <span className="message">{log.message}</span>
                  </div>
                ))
              )}
              <div ref={logsEndRef} />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
