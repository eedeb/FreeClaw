<#
.SYNOPSIS
    FreeClaw - Windows installer. The counterpart of install.sh / install-mac.sh.

.DESCRIPTION
    irm https://freeclaw.eedeb.dev/install.ps1 | iex

    Downloads the portable build, unpacks it into %LOCALAPPDATA%\FreeClaw,
    seeds .env, and starts the notification-area app. No administrator rights
    at any point, and nothing needs to be installed first - the package brings
    its own Python.

    Why this replaced the .exe installer FreeClaw used to ship: a file
    downloaded by a *browser* gets Mark of the Web, and an unsigned .exe
    carrying MotW is met with "Windows protected your PC" (or "Open File -
    Security Warning") where the Run button is hidden behind More info. Most
    people read that as a broken download. PowerShell's own downloads do not
    carry MotW, so this route has no prompt at all.

    Re-running it is the update path: user data is never overwritten.

.PARAMETER Version
    Version to install, e.g. 0.1.0. Default: the latest GitHub release.

.PARAMETER InstallDir
    Where to install. Default: %LOCALAPPDATA%\FreeClaw.

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

.PARAMETER ZipUrl
    Install from this zip instead of the latest GitHub release - a mirror, an
    internal host, or a build you produced yourself. Its .sha256 is fetched
    alongside it when one is published there.

.NOTES
    Piped through `iex` there is nowhere to put parameters, so each one also
    reads an environment variable: FREECLAW_VERSION, FREECLAW_DIR,
    FREECLAW_PASSWORD, FREECLAW_NO_START, FREECLAW_NO_SHORTCUT,
    FREECLAW_NO_PATH, FREECLAW_AUTOSTART, FREECLAW_ZIP_URL.
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$InstallDir,
    [string]$Password,
    [switch]$NoStart,
    [switch]$NoShortcut,
    [switch]$NoPath,
    [switch]$Autostart,
    [string]$ZipUrl
)

$ErrorActionPreference = "Stop"

# Invoke-WebRequest renders a progress bar by writing to the console on every
# chunk, which on Windows PowerShell makes a 90MB download several times
# slower than it needs to be.
$ProgressPreference = "SilentlyContinue"

$Repo = "eedeb/FreeClaw"
$AssetPattern = "FreeClaw-*-win64.zip"

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

$Version     = Env-Or $Version     "FREECLAW_VERSION"
$InstallDir  = Env-Or $InstallDir  "FREECLAW_DIR"
$Password    = Env-Or $Password    "FREECLAW_PASSWORD"
$NoStart     = Env-Flag $NoStart    "FREECLAW_NO_START"
$NoShortcut  = Env-Flag $NoShortcut "FREECLAW_NO_SHORTCUT"
$NoPath      = Env-Flag $NoPath     "FREECLAW_NO_PATH"
$Autostart   = Env-Flag $Autostart  "FREECLAW_AUTOSTART"
$ZipUrl      = Env-Or $ZipUrl       "FREECLAW_ZIP_URL"

if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "FreeClaw" }

# -- output ---------------------------------------------------
function Step($text) { Write-Host ""; Write-Host "  $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "     $text" -ForegroundColor DarkGray }
function Ok($text)   { Write-Host "     $text" -ForegroundColor Green }
function Warn($text) { Write-Host "     ! $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host ""; Write-Host "  x $text" -ForegroundColor Red; Write-Host ""; exit 1 }

Write-Host ""
Write-Host "  FreeClaw" -ForegroundColor Green -NoNewline
Write-Host " - the AI agent that doesn't burn your money"
Write-Host ""

# -- 1. preflight ---------------------------------------------
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Die "PowerShell 5 or newer is required (found $($PSVersionTable.PSVersion))."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    Die "FreeClaw ships a 64-bit build only; this is a 32-bit Windows."
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    Die "Windows 10 or newer is required."
}
# Old Windows PowerShell defaults to SSL3/TLS1.0, which github.com refuses.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$IsUpgrade = Test-Path (Join-Path $InstallDir "src\agent.py")

# -- 2. work out what to download -----------------------------
$headers = @{ "User-Agent" = "FreeClaw-Installer" }
$expectedSize = 0

if ($ZipUrl) {
    Step "Installing from the zip you gave me"
    Info $ZipUrl
    $downloadUrl = $ZipUrl
    $shaUrl = $ZipUrl + ".sha256"
    $assetName = [IO.Path]::GetFileName(($ZipUrl -split '\?')[0])
} else {
    Step "Finding the latest release"
    try {
        if ($Version) {
            $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/tags/v$Version" -Headers $headers
        } else {
            $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers
        }
    } catch {
        Die "Couldn't reach the GitHub releases API: $($_.Exception.Message)"
    }

    $Version = ($rel.tag_name -replace '^v', '')
    $asset = $rel.assets | Where-Object { $_.name -like $AssetPattern } | Select-Object -First 1
    if (-not $asset) {
        Die ("Release v$Version has no $AssetPattern asset. Releases from " +
             "before FreeClaw switched to a zip package only carry the old " +
             "FreeClaw-Setup.exe; pick a newer version, or point -ZipUrl at a " +
             "package you have built yourself.")
    }
    $sha = $rel.assets | Where-Object { $_.name -eq ($asset.name + ".sha256") } | Select-Object -First 1
    $downloadUrl = $asset.browser_download_url
    $expectedSize = $asset.size
    $assetName = $asset.name
    $shaUrl = $null
    if ($sha) { $shaUrl = $sha.browser_download_url }
    Ok "FreeClaw $Version"
}
if ($IsUpgrade) { Info "upgrading the install already in $InstallDir" }

# -- 3. download ----------------------------------------------
if ($expectedSize) { Step "Downloading ($([math]::Round($expectedSize / 1MB, 1)) MB)" }
else               { Step "Downloading" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("freeclaw-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$zip = Join-Path $tmp $assetName
try {
    Invoke-WebRequest $downloadUrl -OutFile $zip -UseBasicParsing -Headers $headers
} catch {
    Die "Download failed: $($_.Exception.Message)"
}

# Verify before unpacking. Preferably against the published checksum; failing
# that against the size the release says it should be, which still catches a
# truncated download or an error page served with HTTP 200 - the exact thing
# that once had freeclaw.eedeb.dev handing out 3KB of HTML named .exe.
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$expected = $null
if ($shaUrl) {
    try {
        # .Content is a string for a text/* response and a byte[] otherwise,
        # and a .sha256 is served as application/octet-stream by GitHub and by
        # most static hosts - so decode rather than assuming. Left as bytes,
        # `-split` stringifies each one and the "expected hash" comes out as
        # the number 99, which is the letter c.
        $raw = (Invoke-WebRequest $shaUrl -UseBasicParsing -Headers $headers).Content
        if ($raw -is [byte[]]) { $raw = [System.Text.Encoding]::ASCII.GetString($raw) }
        $candidate = ([string]$raw).Trim() -split '\s+' | Select-Object -First 1
        # Only trust something that is actually a SHA-256. A 404 page, an HTML
        # redirect or a truncated file would otherwise be compared against the
        # real hash and fail the install for the wrong reason.
        if ($candidate -match '^[0-9a-fA-F]{64}$') { $expected = $candidate.ToLower() }
    } catch {
        $expected = $null   # no checksum published next to this zip
    }
}
if ($expected) {
    if ($actual -ne $expected) {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Die "Checksum mismatch - the download is corrupt or has been tampered with.`n     expected $expected`n     got      $actual"
    }
    Ok "sha256 verified"
} elseif ($expectedSize -and (Get-Item $zip).Length -ne $expectedSize) {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Die "Download is $((Get-Item $zip).Length) bytes, expected $expectedSize."
} else {
    Warn "no published checksum to check against"
}

# A zip always starts "PK". Anything else is a login page, a proxy error or a
# half-written file, and unpacking it would scatter nonsense over the install.
$magic = [System.IO.File]::ReadAllBytes($zip)[0..1]
if ($magic[0] -ne 0x50 -or $magic[1] -ne 0x4B) {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Die "That download is not a zip file. Check the URL in a browser."
}

# -- 4. stop a running FreeClaw -------------------------------
# Same mechanism as the .exe installer: windows/tray.py writes freeclaw.pid at
# startup. Killing by PID rather than by matching process paths keeps this free
# of quoting hazards - an install path is user-controlled. The IMAGENAME filter
# is the safety net against a stale pid file naming a recycled PID.
$pidFile = Join-Path $InstallDir "freeclaw.pid"
if (Test-Path $pidFile) {
    Step "Stopping the running FreeClaw"
    $thePid = (Get-Content $pidFile -First 1).Trim()
    if ($thePid -match '^\d+$') {
        & taskkill.exe /F /T /PID $thePid /FI "IMAGENAME eq pythonw.exe" 2>&1 | Out-Null
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2   # Windows releases file handles asynchronously
    Ok "stopped"
}

# -- 5. unpack ------------------------------------------------
Step "Installing to $InstallDir"
$unpack = Join-Path $tmp "tree"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $unpack)

# With -ZipUrl there was no release to read a version number off, so take it
# from the package itself.
if (-not $Version) {
    $versionFile = Join-Path $unpack "VERSION"
    if (Test-Path $versionFile) { $Version = (Get-Content $versionFile -Raw).Trim() }
    else { $Version = "unknown" }
}

# Never copied over an existing install: these are the user's, not ours.
#   .env              password, providers, MCP servers, install id
#   Flask\static      every chat, upload, context.md - and the Setup Wizard,
#                     which is a live user folder once someone has talked to it
#   logs              history worth keeping across an upgrade
#   browser-profiles  live signed-in browser sessions
$preserve = @(".env", "Flask\static", "logs", "browser-profiles")

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
foreach ($item in Get-ChildItem $unpack -Force) {
    $target = Join-Path $InstallDir $item.Name
    if ($item.Name -eq "Flask") {
        # Flask\ itself must be updated (templates, main.py) while Flask\static
        # inside it is left alone, so this one is merged rather than replaced.
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($sub in Get-ChildItem $item.FullName -Force) {
            $subTarget = Join-Path $target $sub.Name
            if ($sub.Name -eq "static" -and (Test-Path $subTarget)) { continue }
            Copy-Item $sub.FullName -Destination $subTarget -Recurse -Force
        }
        continue
    }
    if (($preserve -contains $item.Name) -and (Test-Path $target)) { continue }
    Copy-Item $item.FullName -Destination $target -Recurse -Force
}
Ok "files in place"

# FreeClaw used to ship as an Inno Setup .exe, which registered itself in
# Add/Remove Programs and left unins000.exe behind. Installing over one of
# those works - it is the same directory layout - but the Windows uninstall
# entry would still be there afterwards, pointing at an uninstaller for files
# this script now manages. Retire it, quietly.
$uninsExe = Join-Path $InstallDir "unins000.exe"
if (Test-Path $uninsExe) {
    Step "Retiring the old .exe install"
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
    Get-ChildItem $key -ErrorAction SilentlyContinue | ForEach-Object {
        $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($props.DisplayName -eq "FreeClaw" -and $props.InstallLocation -and
            $props.InstallLocation.TrimEnd('\') -ieq $InstallDir.TrimEnd('\')) {
            Remove-Item $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue
            Ok "removed the Add/Remove Programs entry"
        }
    }
    Remove-Item $uninsExe -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $InstallDir "unins000.dat") -Force -ErrorAction SilentlyContinue
    Ok "old uninstaller removed - use uninstall.ps1 from now on"
}

# -- 6. .env --------------------------------------------------
Step "Configuring"
$python = Join-Path $InstallDir "python\python.exe"
$envFile = Join-Path $InstallDir ".env"
$writeEnv = Join-Path $InstallDir "windows\write_env.py"

$generated = $null
if ($Password) {
    # Through a file, never an argument: process arguments are readable by
    # anything that can enumerate processes, and this guards the whole web UI.
    # write_env.py deletes it after reading.
    $pwFile = Join-Path $tmp "pw"
    Set-Content -LiteralPath $pwFile -Value $Password -NoNewline -Encoding ascii
    & $python $writeEnv --env $envFile --password-file $pwFile --telemetry 0 | Out-Null
} else {
    $out = & $python $writeEnv --env $envFile --generate-password --telemetry 0
    $line = $out | Where-Object { $_ -like "FC_PASSWORD=*" }
    if ($line) { $generated = ($line -replace '^FC_PASSWORD=', '') }
}
Ok ".env ready"

# -- 7. shortcut, PATH, autostart -----------------------------
$pythonw = Join-Path $InstallDir "python\pythonw.exe"
$trayScript = Join-Path $InstallDir "windows\tray.py"
$icon = Join-Path $InstallDir "windows\freeclaw.ico"

if (-not $NoShortcut) {
    $programs = [Environment]::GetFolderPath("Programs")
    $lnk = Join-Path $programs "FreeClaw.lnk"
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
    $binDir = Join-Path $InstallDir "bin"
    $key = "HKCU:\Environment"
    $current = (Get-ItemProperty -Path $key -Name Path -ErrorAction SilentlyContinue).Path
    if ($null -eq $current) { $current = "" }
    $entries = $current -split ';' | Where-Object { $_.Trim() -ne "" }
    $normalised = $entries | ForEach-Object { $_.Trim().TrimEnd('\').ToLower() }
    if ($normalised -notcontains $binDir.TrimEnd('\').ToLower()) {
        $newPath = (@($entries) + $binDir) -join ';'
        Set-ItemProperty -Path $key -Name Path -Value $newPath -Type ExpandString
        Ok "added $binDir to PATH (open a new terminal to pick it up)"
    } else {
        Info "already on PATH"
    }
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if ($Autostart) {
    # Must match the command windows/tray.py writes from its own "Start with
    # Windows" menu item, or the two would disagree about whether it is on.
    Set-ItemProperty -Path $runKey -Name "FreeClaw" `
        -Value ('"' + $pythonw + '" "' + $trayScript + '"')
    Ok "will start when you sign in"
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

# -- 8. start -------------------------------------------------
if (-not $NoStart) {
    Step "Starting FreeClaw"
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $trayScript + '"') `
                  -WorkingDirectory $InstallDir
    $up = $false
    foreach ($i in 1..45) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:6767/login" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $up = $true; break }
        } catch { }
    }
    if ($up) { Ok "running at http://127.0.0.1:6767" }
    else { Warn "not answering yet - check $InstallDir\logs\tray.log" }
}

# -- done -----------------------------------------------------
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
Write-Host ""
Write-Host "  The tray icon is under the ^ chevron at the right of the taskbar." -ForegroundColor DarkGray
Write-Host "  For the agent's bash tool, install Git for Windows." -ForegroundColor DarkGray
Write-Host ""
