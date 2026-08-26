<#
.SYNOPSIS
    FreeClaw - Windows installer. The counterpart of install.sh / install-mac.sh.

.DESCRIPTION
    irm https://freeclaw.eedeb.dev/install.ps1 | iex

    Clones FreeClaw into %LOCALAPPDATA%\FreeClaw, fetches a private copy of
    Python for it, installs the dependencies, and starts the notification-area
    app. No administrator rights at any point.

    Nothing is hosted anywhere except this script: the source comes from the
    GitHub repo and the interpreter comes from python.org, so a release never
    has to be cut or an artifact uploaded for an install to work.

    Python is a *private* copy under the install directory - the embeddable
    distribution, the same one install.sh's virtualenv plays the part of. It is
    not added to PATH and does not touch any Python you already have. That is
    also why FreeClaw shells out to `sys.executable -m ...` happily:
    src/mcp_client.py spawns the built-in browser MCP server that way.

    Re-running it is the update path: code is refreshed from the repo and user
    data is never touched.

.PARAMETER InstallDir
    Where to install. Default: %LOCALAPPDATA%\FreeClaw.

.PARAMETER Branch
    Branch to track. Default: main.

.PARAMETER Password
    Web UI password. Default: generated and printed once.

.PARAMETER NoStart
    Install but do not launch FreeClaw afterwards.

.PARAMETER NoShortcut
    Skip the Start Menu shortcut.

.PARAMETER NoPath
    Do not add the `freeclaw` command to PATH.

.PARAMETER Autostart
    Start FreeClaw when you sign in.

.NOTES
    Piped through `iex` there is nowhere to put parameters, so each one also
    reads an environment variable: FREECLAW_DIR, FREECLAW_BRANCH,
    FREECLAW_PASSWORD, FREECLAW_NO_START, FREECLAW_NO_SHORTCUT,
    FREECLAW_NO_PATH, FREECLAW_AUTOSTART.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$Branch,
    [string]$Password,
    [switch]$NoStart,
    [switch]$NoShortcut,
    [switch]$NoPath,
    [switch]$Autostart
)

$ErrorActionPreference = "Stop"

# Invoke-WebRequest draws a progress bar by writing to the console on every
# chunk, which on Windows PowerShell makes a large download several times
# slower than it needs to be.
$ProgressPreference = "SilentlyContinue"

$RepoUrl = "https://github.com/eedeb/FreeClaw"
$PythonVersion = "3.12.8"

# -- parameters, or the environment when piped through iex ----
function Env-Or($value, $name) {
    if ($value) { return $value }
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v }
    return $null
}
function Env-Flag($switch, $name) {
    if ($switch) { return $true }
    $v = [Environment]::GetEnvironmentVariable($name)
    return [bool]($v -and $v -ne "0" -and $v -ne "false")
}

$InstallDir = Env-Or $InstallDir "FREECLAW_DIR"
$Branch     = Env-Or $Branch     "FREECLAW_BRANCH"
$Password   = Env-Or $Password   "FREECLAW_PASSWORD"
$NoStart    = Env-Flag $NoStart    "FREECLAW_NO_START"
$NoShortcut = Env-Flag $NoShortcut "FREECLAW_NO_SHORTCUT"
$NoPath     = Env-Flag $NoPath     "FREECLAW_NO_PATH"
$Autostart  = Env-Flag $Autostart  "FREECLAW_AUTOSTART"

if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "FreeClaw" }
if (-not $Branch)     { $Branch = "main" }

# -- output ---------------------------------------------------
function Step($text) { Write-Host ""; Write-Host "  $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "     $text" -ForegroundColor DarkGray }
function Ok($text)   { Write-Host "     $text" -ForegroundColor Green }
function Warn($text) { Write-Host "     ! $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host ""; Write-Host "  x $text" -ForegroundColor Red; Write-Host ""; exit 1 }

# Every external command goes through this, for one specific reason: Windows
# PowerShell turns a native command's stderr into ErrorRecords when it is
# redirected, and with $ErrorActionPreference = 'Stop' the first line git
# writes to stderr - "From https://github.com/..." on a perfectly *successful*
# fetch - aborts the whole script. Relaxing the preference for the duration of
# the call and then checking the real exit code is what makes that survivable.
# Output is left in $script:Out for the callers that want it.
$script:Out = @()
function Native($exe, [string[]]$arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $script:Out = @(& $exe @arguments 2>&1 | ForEach-Object { "$_" })
    } finally {
        $ErrorActionPreference = $prev
    }
    return $LASTEXITCODE
}

Write-Host ""
Write-Host "  FreeClaw" -ForegroundColor Green -NoNewline
Write-Host " - the AI agent that doesn't burn your money"
Write-Host ""

# -- 1. preflight ---------------------------------------------
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Die "PowerShell 5 or newer is required (found $($PSVersionTable.PSVersion))."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    Die "FreeClaw needs 64-bit Windows."
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    Die "Windows 10 or newer is required."
}
# Windows PowerShell defaults to SSL3/TLS1.0, which github.com and python.org
# both refuse.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    Die ("Git is required and isn't installed.`n" +
         "     Install it with:  winget install --id Git.Git -e`n" +
         "     or from https://git-scm.com/download/win , then run this again.`n" +
         "     (FreeClaw's bash tool wants Git for Windows anyway.)")
}

$IsUpgrade = Test-Path (Join-Path $InstallDir ".git")

# -- 2. stop a running FreeClaw -------------------------------
# windows/tray.py writes freeclaw.pid at startup. Killing by PID rather than by
# matching process paths keeps this free of quoting hazards - an install path is
# user-controlled. The IMAGENAME filter is the safety net against a stale pid
# file naming a recycled PID.
$pidFile = Join-Path $InstallDir "freeclaw.pid"
if (Test-Path $pidFile) {
    Step "Stopping the running FreeClaw"
    $thePid = (Get-Content $pidFile -First 1).Trim()
    if ($thePid -match '^\d+$') {
        Native "taskkill.exe" @("/F", "/T", "/PID", $thePid, "/FI", "IMAGENAME eq pythonw.exe") | Out-Null
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2   # Windows releases file handles asynchronously
    Ok "stopped"
}

# -- 3. source --------------------------------------------------
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Push-Location $InstallDir
try {
    if ($IsUpgrade) {
        Step "Updating from $RepoUrl"
        if ((Native $git @("fetch", "--depth", "1", "origin", $Branch)) -ne 0) {
            Die "git fetch failed - no network, or the repo moved."
        }

        # Path by path, never a bare checkout of everything.
        # Flask/static is where every chat, upload and context.md lives, and the
        # Setup Wizard inside it is a *tracked* folder that becomes a live user
        # the moment somebody talks to it - a plain `git checkout` would reset
        # their conversation. Same reasoning as update.sh on Linux.
        foreach ($path in @("src", "models", "windows", "Flask/main.py",
                            "Flask/templates", "requirements.txt", "VERSION",
                            "uninstall.ps1")) {
            Native $git @("checkout", "origin/$Branch", "--", $path) | Out-Null
        }
        # Move HEAD so `git log` is honest next time without touching the tree.
        Native $git @("reset", "--soft", "origin/$Branch") | Out-Null
        Ok "source updated"
    } else {
        Step "Cloning $RepoUrl"
        # init + fetch rather than `git clone`, because the directory may
        # already exist: an old .exe install lived here, or someone made the
        # folder first. clone refuses a non-empty target; this does not.
        if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
            Native $git @("init", "-q") | Out-Null
            Native $git @("remote", "add", "origin", $RepoUrl) | Out-Null
        }
        if ((Native $git @("fetch", "--depth", "1", "origin", $Branch)) -ne 0) {
            Die "Couldn't fetch $RepoUrl - check your network.`n     $($script:Out -join "`n     ")"
        }
        if ((Native $git @("checkout", "-f", "-B", $Branch, "origin/$Branch")) -ne 0) {
            Die "git checkout failed.`n     $($script:Out -join "`n     ")"
        }
        Ok "source cloned"
    }
} finally {
    Pop-Location
}

$Version = "unknown"
$versionFile = Join-Path $InstallDir "VERSION"
if (Test-Path $versionFile) { $Version = (Get-Content $versionFile -Raw).Trim() }

# -- 4. python --------------------------------------------------
# The embeddable distribution: a private interpreter under the install
# directory, playing the part install.sh's virtualenv plays on Linux. Skipped
# when it is already there, so re-running to update is quick.
$PyDir = Join-Path $InstallDir "python"
$PyExe = Join-Path $PyDir "python.exe"
if (-not (Test-Path $PyExe)) {
    Step "Fetching Python $PythonVersion"
    $zipName = "python-$PythonVersion-embed-amd64.zip"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("freeclaw-py-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    $pyZip = Join-Path $tmp $zipName
    try {
        Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/$zipName" `
            -OutFile $pyZip -UseBasicParsing
    } catch {
        Die "Couldn't download Python: $($_.Exception.Message)"
    }
    New-Item -ItemType Directory -Path $PyDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($pyZip, $PyDir)
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

    # The embeddable build ships a ._pth that REPLACES normal path computation:
    # no site-packages, and no entry for the app. Two edits fix both.
    $pth = Get-ChildItem -Path $PyDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) { Die "The Python download is missing its ._pth file." }
    $lines = Get-Content $pth.FullName
    $lines = $lines | ForEach-Object {
        if ($_ -match '^\s*#\s*import\s+site\s*$') { "import site" } else { $_ }
    }
    if ($lines -notcontains "import site") { $lines += "import site" }
    # Paths in a ._pth resolve relative to the file, and the interpreter lives
    # one level down in python\, so ".." is the install root - the directory
    # holding Flask\ and src\. Without it `python -m Flask.main` cannot see its
    # own package.
    if ($lines -notcontains "..") { $lines += ".." }
    Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII

    $getPip = Join-Path $PyDir "get-pip.py"
    Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
    Native $PyExe @($getPip, "--no-warn-script-location", "--no-cache-dir") | Out-Null
    Remove-Item $getPip -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path (Join-Path $PyDir "Lib\site-packages\pip"))) {
        Die "Couldn't bootstrap pip into the bundled Python."
    }
    Ok "python $PythonVersion installed privately"
} else {
    Info "using the Python already in $PyDir"
}

# -- 5. dependencies --------------------------------------------
Step "Installing dependencies"
Info "this is the slow part on a first install"
$code = Native $PyExe @("-m", "pip", "install", "--no-cache-dir",
                        "--no-warn-script-location", "--disable-pip-version-check",
                        "-r", (Join-Path $InstallDir "requirements.txt"),
                        "-r", (Join-Path $InstallDir "windows\requirements-windows.txt"))
if ($code -ne 0) {
    $script:Out | Where-Object { $_ -match 'ERROR|error:' } | Select-Object -Last 5 |
        ForEach-Object { Info $_ }
    Die "pip install failed - see the errors above."
}
Ok "dependencies installed"

# models/run_model.py asks for this table on import, so an install that first
# runs offline would lose the intent classifier on every turn.
if ((Native $PyExe @("-c", "import nltk; nltk.data.find('tokenizers/punkt_tab')")) -ne 0) {
    Native $PyExe @("-c", "import nltk; nltk.download('punkt_tab', download_dir=r'$PyDir\nltk_data', quiet=True)") | Out-Null
}
Ok "classifier data ready"

# -- 6. the freeclaw commands -----------------------------------
# Their own directory, because that is what goes on PATH - everything else
# under the install root would come with it, and putting python.exe and
# tray.py on a user's PATH is not something an app should do behind their back.
$binDir = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
foreach ($shim in @("freeclaw.cmd", "freeclaw-tray.cmd")) {
    Copy-Item (Join-Path $InstallDir "windows\$shim") -Destination $binDir -Force
}

# -- 7. .env ----------------------------------------------------
Step "Configuring"
$envFile = Join-Path $InstallDir ".env"
$writeEnv = Join-Path $InstallDir "windows\write_env.py"
$generated = $null
if ($Password) {
    # Through a file, never an argument: process arguments are readable by
    # anything that can enumerate processes, and this guards the whole web UI.
    # write_env.py deletes it after reading.
    $pwFile = Join-Path ([IO.Path]::GetTempPath()) ("fc-" + [guid]::NewGuid().ToString("N"))
    Set-Content -LiteralPath $pwFile -Value $Password -NoNewline -Encoding ascii
    Native $PyExe @($writeEnv, "--env", $envFile, "--password-file", $pwFile, "--telemetry", "0") | Out-Null
} else {
    Native $PyExe @($writeEnv, "--env", $envFile, "--generate-password", "--telemetry", "0") | Out-Null
    $line = $script:Out | Where-Object { $_ -like "FC_PASSWORD=*" }
    if ($line) { $generated = ($line -replace '^FC_PASSWORD=', '') }
}
Ok ".env ready"

# The marker uninstall.ps1 keys off. An install is a clone of the repo, so it
# looks exactly like a developer's checkout from the outside - this file is the
# only thing that tells them apart. Gitignored, so a checkout never has one.
Set-Content -LiteralPath (Join-Path $InstallDir ".freeclaw-install") -Encoding ascii -Value @(
    "# Written by install.ps1. Its presence marks this directory as a FreeClaw",
    "# install rather than a source checkout; uninstall.ps1 looks for it.",
    "version=$Version",
    "installed=$(Get-Date -Format s)"
)

# FreeClaw used to ship as an Inno Setup .exe, which registered itself in
# Add/Remove Programs and left unins000.exe behind. Installing over one of
# those works - same directory - but the Windows uninstall entry would still be
# there, pointing at an uninstaller for files this script now manages.
$uninsExe = Join-Path $InstallDir "unins000.exe"
if (Test-Path $uninsExe) {
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
    Get-ChildItem $key -ErrorAction SilentlyContinue | ForEach-Object {
        $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($props.DisplayName -eq "FreeClaw" -and $props.InstallLocation -and
            $props.InstallLocation.TrimEnd('\') -ieq $InstallDir.TrimEnd('\')) {
            Remove-Item $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item $uninsExe -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $InstallDir "unins000.dat") -Force -ErrorAction SilentlyContinue
    Ok "retired the old .exe install - uninstall.ps1 is the uninstaller now"
}

# -- 8. shortcut, PATH, autostart -------------------------------
$pythonw = Join-Path $PyDir "pythonw.exe"
$trayScript = Join-Path $InstallDir "windows\tray.py"
$icon = Join-Path $InstallDir "windows\freeclaw.ico"

if (-not $NoShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "FreeClaw.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pythonw
    $s.Arguments = '"' + $trayScript + '"'
    $s.WorkingDirectory = $InstallDir
    $s.IconLocation = $icon
    $s.Description = "Run FreeClaw in the notification area"
    $s.Save()
    Ok "Start Menu shortcut"
}

if (-not $NoPath) {
    # HKCU\Environment\Path is the user's own PATH and one careless write stops
    # every command on the machine resolving. So: read it unexpanded (it
    # routinely contains %USERPROFILE%), compare whole entries, and only write
    # when our bin directory is genuinely absent.
    $key = "HKCU:\Environment"
    $current = (Get-ItemProperty -Path $key -Name Path -ErrorAction SilentlyContinue).Path
    if ($null -eq $current) { $current = "" }
    $entries = $current -split ';' | Where-Object { $_.Trim() -ne "" }
    $normalised = $entries | ForEach-Object { $_.Trim().TrimEnd('\').ToLower() }
    if ($normalised -notcontains $binDir.TrimEnd('\').ToLower()) {
        Set-ItemProperty -Path $key -Name Path -Value ((@($entries) + $binDir) -join ';') `
                         -Type ExpandString
        Ok "added $binDir to PATH (open a new terminal to pick it up)"
    }
}

if ($Autostart) {
    # Must match the command windows/tray.py writes from its own "Start with
    # Windows" menu item, or the two would disagree about whether it is on.
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "FreeClaw" -Value ('"' + $pythonw + '" "' + $trayScript + '"')
    Ok "will start when you sign in"
}

# -- 9. start ---------------------------------------------------
if (-not $NoStart) {
    Step "Starting FreeClaw"
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $trayScript + '"') `
                  -WorkingDirectory $InstallDir
    $up = $false
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:6767/login" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $up = $true; break }
        } catch { }
    }
    if ($up) { Ok "running at http://127.0.0.1:6767" }
    else { Warn "not answering yet - check $InstallDir\logs\tray.log" }
}

# -- done -------------------------------------------------------
Write-Host ""
Write-Host "  ----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
if ($IsUpgrade) {
    Write-Host "  Updated to FreeClaw $Version." -ForegroundColor Green
    Write-Host "  Your chats, files and settings were left untouched." -ForegroundColor DarkGray
} else {
    Write-Host "  FreeClaw $Version is installed." -ForegroundColor Green
}
Write-Host ""
Write-Host "  Open        " -NoNewline -ForegroundColor DarkGray
Write-Host "http://127.0.0.1:6767"
if ($generated) {
    Write-Host "  Password    " -NoNewline -ForegroundColor DarkGray
    Write-Host $generated -ForegroundColor Green
    Write-Host "              (change it in Settings -> Environment)" -ForegroundColor DarkGray
}
Write-Host "  Terminal    " -NoNewline -ForegroundColor DarkGray
Write-Host "freeclaw"
Write-Host "  Update      " -NoNewline -ForegroundColor DarkGray
Write-Host "run this installer again, or Settings -> Update FreeClaw"
Write-Host "  Uninstall   " -NoNewline -ForegroundColor DarkGray
Write-Host "& `"$InstallDir\uninstall.ps1`""
Write-Host ""
Write-Host "  The tray icon is under the ^ chevron at the right of the taskbar." -ForegroundColor DarkGray
Write-Host ""
