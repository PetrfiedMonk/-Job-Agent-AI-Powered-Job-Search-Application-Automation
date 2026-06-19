# Job Agent Launcher - PowerShell Version
# Run: .\start_job_agent.ps1 [mode]
# Modes: search, apply, run, test-profile
#
# SETUP: Before first use, set your API key:
#   [System.Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY","sk-ant-YOUR_KEY_HERE",[System.EnvironmentVariableTarget]::User)

param(
    [string]$mode = "search"
)

# Check if API key is set
if (-not $env:ANTHROPIC_API_KEY) {
    Write-Host ""  
    Write-Host "ERROR: ANTHROPIC_API_KEY environment variable not set!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please set it using:" -ForegroundColor Yellow
    Write-Host "  [System.Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY','sk-ant-YOUR_KEY_HERE',[System.EnvironmentVariableTarget]::User)" -ForegroundColor Cyan
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   JOB AGENT - Starting ($mode mode)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

$commands = @{
    "test-profile" = "Test your AI profile synthesis"
    "search" = "Search and score jobs (no applications)"
    "apply" = "Apply to previously queued jobs"
    "run" = "Full pipeline: search -> score -> tailor -> apply (safe mode, no submission)"
    "live" = "Full pipeline with auto-submit enabled (actually submits!)"
}

if ($commands.ContainsKey($mode)) {
    Write-Host "Mode: $($commands[$mode])" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host "Unknown mode: $mode" -ForegroundColor Red
    Write-Host ""
    Write-Host "Available modes:" -ForegroundColor Yellow
    foreach ($key in $commands.Keys) {
        Write-Host "  $key - $($commands[$key])"
    }
    exit 1
}

# Run the appropriate command
switch ($mode) {
    "live" {
        C:/Python313/python.exe -m job_agent.main run --live
    }
    default {
        C:/Python313/python.exe -m job_agent.main $mode
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   Complete! Check output/ folder for results." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to exit"
