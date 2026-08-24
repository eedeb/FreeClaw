<#
.SYNOPSIS
    Build FreeClaw-Setup-<version>.exe — the Windows installer.

.DESCRIPTION
    Assembles a self-contained tree under build\stage and hands it to Inno
    Setup. The tree carries its own Python, so the installed app depends on
    nothing already being on the machine.

    Why the *embeddable* Python distribution and not PyInstaller: FreeClaw
    shells out to `sys.executable -m ...` in two places that matter —
    src/mcp_client.py spawns the built-in browser MCP server that way, and
    src/browser_setup.py drives playwright with it. Frozen into a PyInstaller
    exe, sys.executable is FreeClaw.exe and both calls turn into nonsense.
    A real python.exe keeps them working untouched.

.PARAMETER PythonVersion
    Embeddable Python to bundle. Must be 3.10+ — requirements.txt gates the
    MCP stack on it, and below that FreeClaw installs without MCP support.

.PARAMETER PythonSha256
    Optional expected SHA-256 of the embeddable zip, from python.org's
    download page. Supply it in CI so a compromised or truncated mirror can't
    quietly end up inside a signed installer.

.PARAMETER SkipInstaller
    Stage everything but don't run Inno Setup. Useful for inspecting the tree
    or for testing the tray app straight out of build\stage.

.EXAMPLE
    pwsh -File windows\build.ps1
    pwsh -File windows\build.ps1 -PythonVersion 3.12.8 -SkipInstaller
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.8",
    [string]$PythonSha256,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root  = Split-Path -Parent $Here
$Build = Join-Path $Root  "build"
$Stage = Join-Path $Build "stage"
$Cache = Join-Path $Build "cache"
$Dist  = Join-Path $Root  "dist"

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Info($text)     { Write-Host "    $text" -ForegroundColor DarkGray }
function Ok($text)       { Write-Host "    $text" -ForegroundColor Green }

$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
Write-Host "FreeClaw $Version — Windows installer build" -ForegroundColor White

# ── 1. workspace ─────────────────────────────────────────────
Step 1 "Preparing the staging tree"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
foreach ($d in @($Build, $Stage, $Cache, $Dist)) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
}
Ok "build\stage is clean"

# ── 2. embeddable Python ─────────────────────────────────────
Step 2 "Fetching embeddable Python $PythonVersion"
$zipName = "python-$PythonVersion-embed-amd64.zip"
$zipPath = Join-Path $Cache $zipName
if (-not (Test-Path $zipPath)) {
    $url = "https://www.python.org/ftp/python/$PythonVersion/$zipName"
    Info "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
} else {
    Info "using cached $zipName"
}

if ($PythonSha256) {
    $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash
    if ($actual -ne $PythonSha256.ToUpper()) {
        Remove-Item $zipPath -Force   # don't leave a bad file in the cache
        throw "SHA-256 mismatch for ${zipName}: expected $PythonSha256, got $actual"
    }
    Ok "checksum verified"
} else {
    Info "no -PythonSha256 given; skipping checksum verification"
}

$PyDir = Join-Path $Stage "python"
Expand-Archive -Path $zipPath -DestinationPath $PyDir -Force
$PyExe = Join-Path $PyDir "python.exe"
Ok "extracted to build\stage\python"

# ── 3. make the embeddable build usable ──────────────────────
Step 3 "Configuring the embedded interpreter"
# The embeddable distribution ships a ._pth file that REPLACES normal path
# computation: no site-packages, no PYTHONPATH, and no entry for the app.
# Two edits fix both problems.
$pth = Get-ChildItem -Path $PyDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "no python*._pth in the embeddable distribution" }
$lines = Get-Content $pth.FullName
# (a) turn on site, which is what makes Lib\site-packages exist and pip work.
$lines = $lines | ForEach-Object { if ($_ -match '^\s*#\s*import\s+site\s*$') { "import site" } else { $_ } }
if ($lines -notcontains "import site") { $lines += "import site" }
# (b) put the install root on sys.path. Paths in a ._pth resolve relative to
#     the file, and the interpreter lives one level down in python\, so ".."
#     is <install> — the directory holding Flask\ and src\. Without it
#     `python -m Flask.main` cannot see its own package.
if ($lines -notcontains "..") { $lines += ".." }
Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII
Info "patched $($pth.Name): import site, .."

$getPip = Join-Path $Cache "get-pip.py"
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
}
& $PyExe $getPip --no-warn-script-location --no-cache-dir | Out-Null
if ($LASTEXITCODE -ne 0) { throw "get-pip failed" }
Ok "pip bootstrapped"

# ── 4. dependencies ──────────────────────────────────────────
Step 4 "Installing dependencies"
& $PyExe -m pip install --no-cache-dir --no-warn-script-location `
    -r (Join-Path $Root "requirements.txt") `
    -r (Join-Path $Here "requirements-windows.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok "app and tray dependencies installed"

# ── 5. NLTK word table ───────────────────────────────────────
Step 5 "Baking in the NLTK punkt_tab table"
# models/run_model.py asks for this on import, so an install that first runs
# offline would otherwise lose the intent classifier on every turn — the same
# reasoning as the Dockerfile's bundled copy. sys.prefix\nltk_data is on
# nltk's default search path, and sys.prefix here is the bundled python dir.
& $PyExe -c "import nltk; nltk.download('punkt_tab', download_dir=r'$PyDir\nltk_data', quiet=True)"
if ($LASTEXITCODE -ne 0) { throw "nltk download failed" }
Ok "punkt_tab bundled"

# ── 6. application files ─────────────────────────────────────
Step 6 "Copying application files"
$exclude = @("__pycache__", "*.pyc", ".DS_Store")
foreach ($dir in @("Flask", "src", "models", "windows")) {
    Copy-Item (Join-Path $Root $dir) -Destination $Stage -Recurse -Force -Exclude $exclude
}
foreach ($file in @("VERSION", "requirements.txt", "README.md", "LICENSE")) {
    Copy-Item (Join-Path $Root $file) -Destination $Stage -Force
}
# Flask\static\ is where every user's chats, uploads and context.md live, so
# on a developer's own machine it is full of their conversations. Only the
# shipped "Setup Wizard" sample belongs in an installer — everything else here
# would be packaging personal data and handing it to whoever installs the exe.
$stageStatic = Join-Path $Stage "Flask\static"
if (Test-Path $stageStatic) {
    Get-ChildItem $stageStatic -Force |
        Where-Object { $_.Name -ne "Setup Wizard" } |
        Remove-Item -Recurse -Force
}

# Bytecode and build scratch have no business in a shipped tree.
Get-ChildItem $Stage -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Stage -Recurse -File -Include "*.pyc", ".DS_Store" |
    Remove-Item -Force -ErrorAction SilentlyContinue
# The `freeclaw` CLI shim goes in its own bin\ directory, because that is the
# directory the installer puts on PATH — everything else under the install root
# would come along with it, and putting python.exe and tray.py on a user's PATH
# is not something an app should do behind their back.
$binDir = Join-Path $Stage "bin"
New-Item -ItemType Directory -Path $binDir -Force | Out-Null
Copy-Item (Join-Path $Here "freeclaw.cmd") -Destination $binDir -Force

# The build's own inputs and previews aren't part of the app.
foreach ($junk in @("build.ps1", "installer.iss", "icon-preview.png",
                    "requirements-windows.txt", "freeclaw.cmd")) {
    Remove-Item (Join-Path $Stage "windows\$junk") -Force -ErrorAction SilentlyContinue
}
# Belt and braces: none of these are in the copy list above, but a stray one
# in the staging tree would ship someone's password or signed-in browser
# sessions inside the installer.
foreach ($secret in @(".env", "freeclaw.pid", "browser-profiles", "Flask\.secret_key")) {
    Remove-Item (Join-Path $Stage $secret) -Recurse -Force -ErrorAction SilentlyContinue
}

# logs\ must exist and be writable before the tray's first line of output.
New-Item -ItemType Directory -Path (Join-Path $Stage "logs") -Force | Out-Null
Ok "staged $((Get-ChildItem $Stage -Recurse -File).Count) files"

# ── 7. installer ─────────────────────────────────────────────
if ($SkipInstaller) {
    Step 7 "Skipping Inno Setup (-SkipInstaller)"
    Write-Host "`nStaged tree: $Stage" -ForegroundColor White
    Write-Host "Try it with:  $PyDir\pythonw.exe $Stage\windows\tray.py" -ForegroundColor White
    exit 0
}

Step 7 "Compiling the installer"
$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    # Where a non-admin install lands — `winget install JRSoftware.InnoSetup`
    # without elevation puts it here, and neither of the paths above nor PATH
    # will find it. Worth checking given the rest of this install story is
    # built around not needing administrator rights.
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
    throw ("Inno Setup 6 not found. Install it with:  " +
           "winget install JRSoftware.InnoSetup   (or: choco install innosetup -y)")
}
Info "using $iscc"

& $iscc "/DAppVersion=$Version" "/DStageDir=$Stage" "/DOutputDir=$Dist" `
        (Join-Path $Here "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$setup = Join-Path $Dist "FreeClaw-Setup-$Version.exe"
$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "`nBuilt $setup ($size MB)" -ForegroundColor Green
