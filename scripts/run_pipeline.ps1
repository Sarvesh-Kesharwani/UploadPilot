$ErrorActionPreference = 'Continue'
$api = 'http://127.0.0.1:3000'
$report = 'D:\UploadPilot\youlearn_upload_report.md'
$plog = 'D:\UploadPilot\logs\pipeline.log'
New-Item -ItemType Directory -Force -Path (Split-Path $plog) | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $plog -Value $line
    Write-Output $line
}

function Get-Json($url, $method, $body) {
    $params = @{ Uri = $url; Method = $method; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($body) {
        $params.Body = ($body | ConvertTo-Json -Depth 6)
        $params.ContentType = 'application/json'
    }
    Invoke-RestMethod @params
}

function Ensure-Server {
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $null = Get-Json "$api/api/state" 'GET' $null
            return $true
        } catch {
            Start-Sleep -Seconds 5
        }
    }
    try {
        Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File','D:\UploadPilot\scripts\start_hidden.ps1' -WindowStyle Hidden
        Start-Sleep -Seconds 15
    } catch {
        Log "server restart attempt failed: $($_.Exception.Message)"
    }
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $null = Get-Json "$api/api/state" 'GET' $null
            return $true
        } catch {
            Start-Sleep -Seconds 10
        }
    }
    return $false
}

$jobs = @(
    @{ name = 'PSIR'; space = 'https://app.youlearn.ai/space/08757e83b4194f49'; folder = 'D:\.Courses\PSIR' }
    @{ name = 'CampusX-Genai-Gemini'; space = 'https://app.youlearn.ai/space/96859829e8e443b3?folderId=sf-3f0853d8-6cb1-4c9b-b84e-d039a70ae3c0'; folder = 'D:\1. The Downloads\Campusx-Genai-Gemini' }
    @{ name = 'CampusX-Data-Analytics'; space = 'https://app.youlearn.ai/space/96859829e8e443b3?folderId=sf-cbec77d5-37eb-439e-8b40-19d58886ce5b'; folder = 'D:\1. The Downloads\Campusx-Data-Analytics' }
    @{ name = 'CampusX-n8n'; space = 'https://app.youlearn.ai/space/96859829e8e443b3?folderId=sf-6304272e-8d0c-443f-9c29-3f21500ade35'; folder = 'D:\1. The Downloads\Campusx-n8n' }
    @{ name = 'LLM Engineering'; space = 'https://app.youlearn.ai/space/db22211b805a4a68'; folder = 'D:\.Courses\LLm Engineering Master AI, LLM and Agents' }
    @{ name = 'Agentic AI Engineering'; space = 'https://app.youlearn.ai/space/40a4f507bb614858'; folder = 'D:\.Courses\The Complete Agentic AI Engineering Course (2025)' }
    @{ name = 'Not in Focus'; space = 'https://app.youlearn.ai/space/9a656fd7b51f4a87'; folder = 'D:\.Courses\.not in focus rt now' }
    @{ name = 'FastAPI Udemy'; space = 'https://app.youlearn.ai/space/43a99a9c4fbe4d2b'; folder = 'D:\.Courses\fastapi udemy' }
)

$startTime = Get-Date
$deadline = $startTime.AddHours(12)

$header = @"
# YouLearn upload pipeline report

Started: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (Asia/Kolkata)
Window: 12 hours

## Per-course spaces
| Course | Space ID | Folder (local) |
|---|---|---|
| PSIR | 08757e83b4194f49 | D:\.Courses\PSIR |
| CampusX Genai-Gemini | 96859829e8e443b3 / sf-3f0853d8 | D:\1. The Downloads\Campusx-Genai-Gemini |
| CampusX Data-Analytics | 96859829e8e443b3 / sf-cbec77d5 | D:\1. The Downloads\Campusx-Data-Analytics |
| CampusX n8n | 96859829e8e443b3 / sf-6304272e | D:\1. The Downloads\Campusx-n8n |
| FastAPI Udemy | 43a99a9c4fbe4d2b | D:\.Courses\fastapi udemy |
| LLM Engineering | db22211b805a4a68 | D:\.Courses\LLm Engineering Master AI, LLM and Agents |
| Agentic AI Engineering | 40a4f507bb614858 | D:\.Courses\The Complete Agentic AI Engineering Course (2025) |
| Not in Focus | 9a656fd7b51f4a87 | D:\.Courses\.not in focus rt now |

## Job log
"@
Set-Content -Path $report -Value $header -Encoding UTF8
Log "pipeline started; deadline $deadline"

foreach ($job in $jobs) {
    if ((Get-Date) -gt $deadline) {
        Log "deadline passed before job $($job.name); leaving queued"
        Add-Content -Path $report -Value "`n### $($job.name) - NOT STARTED (window ended)"
        continue
    }
    $attempt = 0
    $done = $false
    while (-not $done -and $attempt -lt 3 -and (Get-Date) -lt $deadline) {
        $attempt++
        Log "job $($job.name) attempt $attempt start"
        $ok = Ensure-Server
        if (-not $ok) {
            Log "job $($job.name) - server unavailable, aborting job"
            break
        }
        try {
            $startBody = @{ space_url = $job.space; folder = $job.folder; mode = 'smart' }
            $null = Get-Json "$api/api/start" 'POST' $startBody
        } catch {
            Log "job $($job.name) start error: $($_.Exception.Message)"
            Start-Sleep -Seconds 20
            continue
        }
        $finished = $false
        $lastState = $null
        $pollErrors = 0
        while (-not $finished) {
            Start-Sleep -Seconds 20
            try {
                $lastState = Get-Json "$api/api/state" 'GET' $null
                $pollErrors = 0
            } catch {
                Log "job $($job.name) state poll error: $($_.Exception.Message)"
                $pollErrors++
                if ($pollErrors -ge 5) {
                    Log "job $($job.name) - server unreachable for 5 polls; breaking to restart"
                    break
                }
                continue
            }
            if (-not $lastState.job) {
                Log "job $($job.name) - no active job in state (server restarted?); breaking to retry"
                break
            }
            if ($lastState.job -and $lastState.job.finished_at) {
                $finished = $true
            }
            if ((Get-Date) -gt $deadline) {
                Log "job $($job.name) - deadline reached while running; cancelling"
                try { $null = Get-Json "$api/api/cancel" 'POST' $null } catch {}
                $finished = $true
            }
        }
        if (-not $lastState -or -not $lastState.job) {
            Log "job $($job.name) - no final state"
            continue
        }
        $items = @($lastState.job.items)
        $uploaded = @($items | Where-Object { $_.status -eq 'uploaded' })
        $skipped = @($items | Where-Object { $_.status -eq 'skipped' })
        $failed = @($items | Where-Object { $_.status -eq 'failed' })
        $queued = @($items | Where-Object { $_.status -notin @('uploaded','skipped','failed') })
        Log "job $($job.name) attempt $attempt done: total=$($items.Count) uploaded=$($uploaded.Count) skipped=$($skipped.Count) failed=$($failed.Count) other=$($queued.Count)"
        $section = "`n### $($job.name) (attempt $attempt, $(Get-Date -Format 'HH:mm:ss'))`n- Total: $($items.Count) | Uploaded: $($uploaded.Count) | Skipped: $($skipped.Count) | Failed: $($failed.Count)"
        if ($failed.Count -gt 0) {
            $section += "`n- Failed files:"
            foreach ($f in $failed) {
                $section += "`n  - $($f.name) -> $($f.error)"
            }
        }
        Add-Content -Path $report -Value $section -Encoding UTF8
        if ($failed.Count -eq 0) {
            $done = $true
        } else {
            Log "job $($job.name) - retrying ($($failed.Count) failed)"
            Start-Sleep -Seconds 15
        }
    }
    if (-not $done) {
        Add-Content -Path $report -Value "`n### $($job.name) - FAILED AFTER RETRIES or window ended" -Encoding UTF8
    }
}

Add-Content -Path $report -Value "`n---`n## Final summary $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -Encoding UTF8
Log "pipeline finished"
