[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$SkipInstall,
    [switch]$CheckOnly,
    [switch]$Stop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$webRoot = Join-Path $projectRoot 'web'
$runtimeRoot = Join-Path $projectRoot '.cache\dashboard'
$statePath = Join-Path $runtimeRoot 'processes.json'
$script:managedProcesses = @()
$startedProcesses = @()

function Save-ProcessState {
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    ConvertTo-Json -InputObject @($script:managedProcesses) -Depth 3 |
        Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Test-OwnedProcess($Record) {
    if ($null -eq $Record -or -not $Record.PSObject.Properties['ProcessId'] -or
        -not $Record.PSObject.Properties['StartedAt']) { return $false }
    $running = Get-Process -Id $Record.ProcessId -ErrorAction SilentlyContinue
    return ($null -ne $running -and
        $running.StartTime.ToUniversalTime().ToString('o') -eq $Record.StartedAt)
}

function Stop-OwnedProcess($Record) {
    if (Test-OwnedProcess $Record) {
        Write-Host "Stopping $($Record.Name)..."
        & taskkill.exe /PID $Record.ProcessId /T /F | Out-Host
        if ($LASTEXITCODE -ne 0 -and (Test-OwnedProcess $Record)) {
            throw "Could not stop $($Record.Name) (PID $($Record.ProcessId))."
        }
    }
}

function Get-RequiredTool([string]$Name) {
    $tool = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if (-not $tool) { throw "Required tool '$Name' was not found. Install it and reopen this launcher." }
    return $tool.Source
}

function Test-ServiceReady([string]$Name) {
    try {
        if ($Name -eq 'backend') {
            $health = Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 5
            $schema = Invoke-RestMethod 'http://127.0.0.1:8000/openapi.json' -TimeoutSec 5
            return ($health.ok -eq $true -and $schema.info.title -eq 'Stock Strategies API')
        }
        $page = Invoke-WebRequest 'http://localhost:3000' -UseBasicParsing -TimeoutSec 5
        return ($page.StatusCode -eq 200 -and $page.Content.Contains('Stock Strategies'))
    } catch {
        return $false
    }
}

function Test-ExistingService([string]$Name, [int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { return $false }
    # An existing dev server may need to compile its first page.
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        if (Test-ServiceReady $Name) {
            Write-Host "Reusing $Name on port $Port."
            return $true
        }
        Start-Sleep -Seconds 1
    }
    throw "Port $Port is occupied, but the expected $Name is not healthy. Check or stop that service first."
}

function Start-DashboardService([string]$Name, [string]$Executable, [string[]]$ServiceArgs, [string]$Directory) {
    Write-Host "Starting $Name in the background..."
    $process = Start-Process -FilePath $Executable -ArgumentList $ServiceArgs `
        -WorkingDirectory $Directory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot "$Name.log") `
        -RedirectStandardError (Join-Path $runtimeRoot "$Name.error.log")
    $record = [pscustomobject]@{
        Name = $Name
        ProcessId = $process.Id
        StartedAt = $process.StartTime.ToUniversalTime().ToString('o')
    }
    $script:managedProcesses += $record
    Save-ProcessState
    return $record
}

function Wait-ServiceReady($Record) {
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-OwnedProcess $Record)) {
            throw "$($Record.Name) exited. See logs in $runtimeRoot."
        }
        if (Test-ServiceReady $Record.Name) { return }
        Start-Sleep -Seconds 1
    }
    throw "$($Record.Name) did not become ready in 90 seconds. See logs in $runtimeRoot."
}

try {
    if (Test-Path -LiteralPath $statePath) {
        # Windows PowerShell 5.1 may emit an empty JSON array as one pipeline object.
        $savedRecords = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $script:managedProcesses = @(foreach ($savedRecord in $savedRecords) {
            if (Test-OwnedProcess $savedRecord) { $savedRecord }
        })
    }
    if ($Stop) {
        foreach ($record in $script:managedProcesses) { Stop-OwnedProcess $record }
        $script:managedProcesses = @()
        Save-ProcessState
        Write-Host 'Stopped launcher-managed services. Reused services were left running.'
        exit 0
    }

    $uvPath = Get-RequiredTool 'uv.exe'
    $nodePath = Get-RequiredTool 'node.exe'
    $npmPath = Get-RequiredTool 'npm.cmd'
    $backendExists = Test-ExistingService 'backend' 8000
    $frontendExists = Test-ExistingService 'frontend' 3000
    if ($CheckOnly) {
        Write-Host 'Startup checks passed. Backend: http://127.0.0.1:8000; dashboard: http://localhost:3000'
        exit 0
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

    if (-not $SkipInstall) {
        if (-not $backendExists) {
            Write-Host 'Checking Python dependencies...'
            Push-Location $projectRoot
            try {
                & $uvPath sync --frozen
                if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' }
            } finally { Pop-Location }
        }
        if (-not $frontendExists) {
            $lockHash = (Get-FileHash -LiteralPath (Join-Path $webRoot 'package-lock.json') -Algorithm SHA256).Hash
            $stampPath = Join-Path $runtimeRoot 'npm-lock.sha256'
            $installedHash = if (Test-Path -LiteralPath $stampPath) { (Get-Content -LiteralPath $stampPath -Raw).Trim() } else { '' }
            if ($installedHash -ne $lockHash -or -not (Test-Path -LiteralPath (Join-Path $webRoot 'node_modules\next\dist\bin\next'))) {
                Write-Host 'Installing frontend dependencies from package-lock.json...'
                Push-Location $webRoot
                try {
                    & $npmPath ci
                    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
                    Set-Content -LiteralPath $stampPath -Value $lockHash -Encoding ASCII
                } finally { Pop-Location }
            }
        }
    }

    if (-not $backendExists) {
        $record = Start-DashboardService 'backend' $uvPath @('run', '--frozen', 'python', '-m', 'uvicorn', 'api.main:app', '--reload', '--port', '8000') $projectRoot
        $startedProcesses += $record
        Wait-ServiceReady $record
    }
    if (-not $frontendExists) {
        $nextCli = Join-Path $webRoot 'node_modules\next\dist\bin\next'
        if (-not (Test-Path -LiteralPath $nextCli)) { throw 'Frontend dependencies are missing. Run without -SkipInstall.' }
        # Quote the CLI path because Start-Process joins ArgumentList into a Windows command line.
        $record = Start-DashboardService 'frontend' $nodePath @(('"' + $nextCli + '"'), 'dev', '--port', '3000') $webRoot
        $startedProcesses += $record
        Wait-ServiceReady $record
    }
    Save-ProcessState
    Write-Host 'Dashboard ready: http://localhost:3000'
    Write-Host "Logs: $runtimeRoot"
    Write-Host 'To stop services started by this launcher: .\start-dashboard.cmd -Stop'
    if (-not $NoBrowser) { Start-Process 'http://localhost:3000' }
} catch {
    Write-Host "Dashboard startup failed: $($_.Exception.Message)" -ForegroundColor Red
    foreach ($record in $startedProcesses) { Stop-OwnedProcess $record }
    $script:managedProcesses = @($script:managedProcesses | Where-Object { Test-OwnedProcess $_ })
    Save-ProcessState
    exit 1
}
