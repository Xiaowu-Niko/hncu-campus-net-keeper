# ==========================================================================
#  Campus Network Keeper - Supervisor / Controller  (PowerShell)
# --------------------------------------------------------------------------
#  All real logic lives here instead of in a .bat file, because cmd.exe
#  quoting and errorlevel handling are too fragile for process supervision.
#
#  Paths are resolved from this script's own location, so the whole folder
#  can be moved / renamed / unzipped anywhere and still work.
#  Python is auto-detected (PATH -> py launcher -> common install dirs);
#  override with the CAMPUS_KEEPER_PYTHON environment variable if needed.
#
#  No scheduled task is used. The supervisor is a plain detached process.
#  Consequence: the keeper does NOT auto-start after a reboot or logoff.
#  Just double-click start_keeper.bat again after restarting the PC.
#
#  Usage (normally called via start_keeper.bat):
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 start
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 stop
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 restart
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 status
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 check
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 visible
#     powershell -ExecutionPolicy Bypass -File keeper.ps1 supervise   (internal)
#
#  This file must stay ASCII-only: Windows PowerShell 5.1 reads a BOM-less
#  .ps1 as ANSI, and non-ASCII text would come out as mojibake.
# ==========================================================================

param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'check', 'visible', 'supervise', 'help')]
    [string]$Action = 'help'
)

$ErrorActionPreference = 'Continue'

# ---- Paths (derived from this script's own location, never hard-coded) ----
$SelfPath = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrEmpty($SelfPath)) { $SelfPath = $PSCommandPath }
if ([string]::IsNullOrEmpty($SelfPath)) { $SelfPath = Join-Path (Get-Location).Path 'keeper.ps1' }
$ScriptDir = Split-Path -Parent $SelfPath
if ([string]::IsNullOrEmpty($ScriptDir)) { $ScriptDir = (Get-Location).Path }

$Target    = Join-Path $ScriptDir 'campus_network_keeper.py'
$ConfigJs  = Join-Path $ScriptDir 'config.json'
$ExampleJs = Join-Path $ScriptDir 'config.example.json'
$LogDir    = Join-Path $ScriptDir 'logs'
$SupLog    = Join-Path $LogDir 'supervisor.log'
$LockFile  = Join-Path $LogDir 'keeper.lock'
$PidFile   = Join-Path $LogDir 'supervisor.pid'
$KeeperLog = Join-Path $ScriptDir 'campus_network_keeper.log'
$RestartDelaySec = 10
$MaxRestartDelaySec = 300

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Sup {
    param([string]$Message)
    $line = '{0} [SUPERVISOR] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    try {
        # Rotate supervisor log if it gets large (keeps last 500 lines)
        if (Test-Path -LiteralPath $SupLog) {
            if ((Get-Item -LiteralPath $SupLog).Length -gt 512KB) {
                $keep = Get-Content -LiteralPath $SupLog -Tail 500 -ErrorAction SilentlyContinue
                Set-Content -Path $SupLog -Value $keep -Encoding UTF8 -ErrorAction SilentlyContinue
            }
        }
        Add-Content -LiteralPath $SupLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

# --------------------------------------------------------------------------
# Python discovery
# --------------------------------------------------------------------------
function Test-PythonWorks {
    param([string]$Exe)
    if ([string]::IsNullOrWhiteSpace($Exe)) { return $false }
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-PythonExe {
    # 1) explicit override
    if ($env:CAMPUS_KEEPER_PYTHON) {
        if (Test-PythonWorks $env:CAMPUS_KEEPER_PYTHON) { return $env:CAMPUS_KEEPER_PYTHON }
    }

    # 2) python.exe already on PATH
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and (Test-PythonWorks $cmd.Source)) { return $cmd.Source }

    # 3) the py launcher (ships with the python.org installer)
    $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        try {
            $resolved = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                $resolved = ($resolved | Select-Object -First 1).Trim()
                if (Test-PythonWorks $resolved) { return $resolved }
            }
        } catch { }
    }

    # 4) common install locations (no assumptions about a specific username)
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python3*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe')
    )
    if (${env:ProgramFiles(x86)}) {
        $patterns += (Join-Path ${env:ProgramFiles(x86)} 'Python3*\python.exe')
    }
    $patterns += 'C:\Python3*\python.exe'
    foreach ($pattern in $patterns) {
        $hit = Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit -and (Test-PythonWorks $hit.FullName)) { return $hit.FullName }
    }

    return $null
}

$PyExe = $null   # resolved lazily, only when we actually need to launch python

function Get-PythonOrFail {
    if ($script:PyExe) { return $script:PyExe }
    $exe = Resolve-PythonExe
    if (-not $exe) {
        Write-Host '[ERROR] Python 3.8+ not found.'
        Write-Host '        Install Python from https://www.python.org/downloads/'
        Write-Host '        and tick "Add python.exe to PATH" during setup.'
        Write-Host '        Already installed elsewhere? Set an env var:'
        Write-Host '          set CAMPUS_KEEPER_PYTHON=C:\path\to\python.exe'
        return $null
    }
    $script:PyExe = $exe
    return $exe
}

# --------------------------------------------------------------------------
# Process lookup
# --------------------------------------------------------------------------
function Test-KeeperProcess {
    <#
      Returns keeper processes (objects with a ProcessId property).
      Matches on the command line so unrelated python jobs are never touched.
      Falls back to the recorded lock-file PID when CIM is unavailable.
    #>
    $found = @()
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" -ErrorAction Stop |
            ForEach-Object {
                if ($_.CommandLine -and $_.CommandLine -like ('*' + (Split-Path -Leaf $Target) + '*')) {
                    $found += [pscustomobject]@{ ProcessId = $_.ProcessId }
                }
            }
        if ($found.Count -gt 0) { return , $found }
    } catch { }

    if (Test-Path -LiteralPath $LockFile) {
        $lockPid = (Get-Content -LiteralPath $LockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($lockPid -and (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)) {
            $found += [pscustomobject]@{ ProcessId = [int]$lockPid }
            return , $found
        }
    }
    return , $found
}

function Get-SupervisorProcesses {
    <#
      Returns supervisor processes. Tries CIM command-line matching first, then
      falls back to the recorded supervisor.pid, so /stop still works when CIM
      is unavailable.
    #>
    $found = @()
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
            ForEach-Object {
                if ($_.CommandLine -and $_.CommandLine -like '*keeper.ps1*' -and $_.CommandLine -like '*supervise*') {
                    $found += [pscustomobject]@{ ProcessId = $_.ProcessId }
                }
            }
        if ($found.Count -gt 0) { return , $found }
    } catch { }

    if (Test-Path -LiteralPath $PidFile) {
        $supPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($supPid) {
            $p = Get-Process -Id $supPid -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -eq 'powershell') {
                $found += [pscustomobject]@{ ProcessId = [int]$supPid }
            }
        }
    }
    return , $found
}

function Stop-Supervisor {
    $procs = Get-SupervisorProcesses
    if ($procs.Count -eq 0) {
        Write-Host '  no supervisor process found'
    } else {
        foreach ($p in $procs) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-Host ('  stopped supervisor pid=' + $p.ProcessId)
            } catch {
                Write-Host ('  could not stop supervisor pid=' + $p.ProcessId + ': ' + $_.Exception.Message)
            }
        }
    }
}

function Stop-Keeper {
    $procs = Test-KeeperProcess
    if ($procs.Count -eq 0) {
        Write-Host '  no keeper process found'
    } else {
        foreach ($p in $procs) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-Host ('  stopped keeper pid=' + $p.ProcessId)
            } catch {
                Write-Host ('  could not stop keeper pid=' + $p.ProcessId + ': ' + $_.Exception.Message)
            }
        }
    }
}

function Show-Status {
    Write-Host '================ KEEPER STATUS ================'
    Write-Host ('  folder   : ' + $ScriptDir)
    $py = Resolve-PythonExe
    if ($py) { Write-Host ('  python   : ' + $py) } else { Write-Host '  python   : NOT FOUND' }
    if (Test-Path -LiteralPath $ConfigJs) {
        Write-Host ('  config   : ' + $ConfigJs)
    } elseif (Test-Path -LiteralPath $ExampleJs) {
        Write-Host '  config   : MISSING config.json (only the example template is present)'
    } else {
        Write-Host '  config   : MISSING'
    }
    Write-Host ''

    Write-Host '--- keeper (python) ---'
    $k = Test-KeeperProcess
    if ($k.Count -eq 0) {
        Write-Host '  not running'
    } else {
        foreach ($p in $k) { Write-Host ('  RUNNING  pid=' + $p.ProcessId) }
    }

    Write-Host ''
    Write-Host '--- supervisor ---'
    $s = Get-SupervisorProcesses
    if ($s.Count -eq 0) { Write-Host '  not running' } else { foreach ($p in $s) { Write-Host ('  RUNNING  pid=' + $p.ProcessId) } }

    if (Test-Path -LiteralPath $LockFile) {
        $lockPid = (Get-Content -LiteralPath $LockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        $alive = $false
        if ($lockPid) { $alive = [bool](Get-Process -Id $lockPid -ErrorAction SilentlyContinue) }
        Write-Host ('  lock pid=' + $lockPid + ' alive=' + $alive)
    }

    Write-Host ''
    Write-Host '--- system proxy (should be untouched by the keeper) ---'
    try {
        $reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        Write-Host ('  ProxyEnable = ' + $reg.ProxyEnable)
        Write-Host ('  ProxyServer = ' + $reg.ProxyServer)
    } catch { Write-Host '  could not read proxy settings' }

    Write-Host ''
    Write-Host '--- supervisor.log (last 10) ---'
    if (Test-Path -LiteralPath $SupLog) { Get-Content -LiteralPath $SupLog -Tail 10 -ErrorAction SilentlyContinue } else { Write-Host '  (none)' }

    Write-Host ''
    Write-Host '--- keeper log (last 8) ---'
    if (Test-Path -LiteralPath $KeeperLog) { Get-Content -LiteralPath $KeeperLog -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue } else { Write-Host '  (none)' }

    Write-Host ''
    Write-Host '==============================================='
}

function Invoke-Supervise {
    <# The supervision loop. Runs until the process is killed. #>
    Write-Sup ('supervisor started (pid=' + $PID + ')')
    Set-Content -LiteralPath $PidFile -Value $PID -Encoding ASCII -ErrorAction SilentlyContinue

    $delay = $RestartDelaySec
    while ($true) {
        try {
            $exe = Get-PythonOrFail
            if (-not $exe) {
                Write-Sup 'python not found; retrying later'
                Start-Sleep -Seconds 60
                continue
            }
            Write-Sup ('starting keeper: ' + $Target)
            $proc = Start-Process -FilePath $exe `
                                  -ArgumentList @('-u', ('"' + $Target + '"')) `
                                  -WorkingDirectory $ScriptDir `
                                  -WindowStyle Hidden -PassThru
            Set-Content -LiteralPath $LockFile -Value $proc.Id -Encoding ASCII -ErrorAction SilentlyContinue
            Write-Sup ('keeper started pid=' + $proc.Id)
            $delay = $RestartDelaySec   # reset backoff on a successful launch

            # Wait for exit. Poll HasExited, and also detect the process vanishing.
            while (-not $proc.HasExited) {
                Start-Sleep -Seconds 5
                try { $proc.Refresh() } catch { break }
            }

            $code = 'unknown'
            try { $code = $proc.ExitCode } catch { }
            Write-Sup ('keeper pid=' + $proc.Id + ' exited (code=' + $code + '); restarting in ' + $delay + 's')

        } catch {
            Write-Sup ('supervisor error: ' + $_.Exception.Message)
            # Back off exponentially so a persistently broken setup does not spin.
            $delay = [Math]::Min($delay * 2, $MaxRestartDelaySec)
            Write-Sup ('backing off to ' + $delay + 's')
        }

        Start-Sleep -Seconds $delay
    }
}

function Invoke-Start {
    # ---- Preflight ------------------------------------------------------
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Host ('[ERROR] Target script not found: ' + $Target)
        Write-Host '        Keep keeper.ps1 and campus_network_keeper.py in the same folder.'
        return 1
    }
    $exe = Get-PythonOrFail
    if (-not $exe) { return 1 }
    if (-not (Test-Path -LiteralPath $ConfigJs)) {
        Write-Host '[WARN] config.json not found.'
        Write-Host ('       Copy config.example.json to config.json and fill in your accounts:')
        Write-Host ('         ' + $ConfigJs)
        if (-not (Test-Path -LiteralPath $ExampleJs)) { return 1 }
    }

    # ---- Refuse to double-start ------------------------------------------
    $existing = Test-KeeperProcess
    if ($existing.Count -gt 0) {
        Write-Host '[WARN] The keeper is already running:'
        foreach ($p in $existing) { Write-Host ('         pid=' + $p.ProcessId) }
        Write-Host '       Run "start_keeper.bat /stop" first to restart it.'
        return 0
    }
    $sup = Get-SupervisorProcesses
    if ($sup.Count -gt 0) {
        Write-Host '[WARN] A supervisor is already running; not starting a second one.'
        return 0
    }

    Write-Host '[INFO] Starting supervisor (detached)...'
    Write-Host ('       python: ' + $exe)
    try {
        Start-Process -FilePath 'powershell.exe' `
                      -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $SelfPath, 'supervise') `
                      -WindowStyle Hidden | Out-Null
        Write-Host '  started detached powershell'
    } catch {
        Write-Host ('[ERROR] could not start supervisor: ' + $_.Exception.Message)
        return 1
    }

    Start-Sleep -Seconds 4
    $k = Test-KeeperProcess
    if ($k.Count -gt 0) {
        Write-Host '[INFO] Keeper is running.'
    } else {
        Write-Host '[WARN] Keeper did not appear yet. Check logs\supervisor.log'
    }
    Write-Host ('[INFO] Log: ' + $SupLog)
    Write-Host '[NOTE] No scheduled task is used, so it will NOT auto-start after a reboot.'
    return 0
}

function Invoke-Stop {
    Write-Host '[INFO] Stopping supervisor...'
    Stop-Supervisor
    Write-Host '[INFO] Stopping keeper...'
    Stop-Keeper
    if (Test-Path -LiteralPath $LockFile) { Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $PidFile)  { Remove-Item -LiteralPath $PidFile  -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    $k = Test-KeeperProcess
    if ($k.Count -eq 0) { Write-Host '[DONE] Stop complete.' } else { Write-Host '[WARN] Keeper still running.' }
    return 0
}

function Invoke-Visible {
    if (-not (Test-Path -LiteralPath $Target)) { Write-Host ('[ERROR] Target not found: ' + $Target); return 1 }
    $exe = Get-PythonOrFail
    if (-not $exe) { return 1 }
    Write-Host '[INFO] Foreground mode. Press Ctrl+C to stop.'
    Write-Host ''
    Push-Location -LiteralPath $ScriptDir
    try {
        & $exe -u $Target
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Write-Host ''
    Write-Host ('[INFO] Exited with code ' + $code)
    return $code
}

function Invoke-Check {
    if (-not (Test-Path -LiteralPath $Target)) { Write-Host ('[ERROR] Target not found: ' + $Target); return 1 }
    $exe = Get-PythonOrFail
    if (-not $exe) { return 1 }
    Push-Location -LiteralPath $ScriptDir
    try {
        & $exe -u $Target --check
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    return $code
}

function Show-Help {
    Write-Host 'Campus Network Keeper - controller'
    Write-Host ''
    Write-Host '   start_keeper.bat            start hidden + auto-restart on crash'
    Write-Host '   start_keeper.bat /visible   foreground run (debug, Ctrl+C stops)'
    Write-Host '   start_keeper.bat /stop      stop keeper + supervisor'
    Write-Host '   start_keeper.bat /restart   stop then start'
    Write-Host '   start_keeper.bat /status    show processes, proxy state, logs'
    Write-Host '   start_keeper.bat /check     validate config + probe the line'
    Write-Host '   start_keeper.bat /help      this help'
    Write-Host ''
    Write-Host '   No scheduled task is used; re-run start_keeper.bat after a reboot.'
    Write-Host ''
}

switch ($Action) {
    'start'     { exit (Invoke-Start) }
    'stop'      { exit (Invoke-Stop) }
    'restart'   { Invoke-Stop | Out-Null; Start-Sleep -Seconds 1; exit (Invoke-Start) }
    'status'    { Show-Status; exit 0 }
    'check'     { exit (Invoke-Check) }
    'visible'   { exit (Invoke-Visible) }
    'supervise' { Invoke-Supervise }     # never returns
    'help'      { Show-Help; exit 0 }
}
