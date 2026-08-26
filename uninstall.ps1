<#
.SYNOPSIS
    FreeClaw - Windows uninstaller. The counterpart of uninstall.sh.

.DESCRIPTION
    A copy of this script is installed alongside FreeClaw, so the usual way to
    run it is from the install itself - no network, and it defaults to
    removing the install it is sitting in:

        & "$env:LOCALAPPDATA\FreeClaw\uninstall.ps1"

    Only install.ps1 is published on the website. If the install is too broken
    to run its own copy, fetch this one from the repo:

        irm https://raw.githubusercontent.com/eedeb/FreeClaw/main/uninstall.ps1 | iex

    Stops FreeClaw, removes the shortcut, the autostart entry and the PATH
    entry, and deletes the program files.

    Your data stays by default. Chats, uploads, context.md, saved browser
    logins, logs and .env are yours, and an uninstaller is the last place that
    should be making that decision for you - pass -Purge if you really want
    them gone.

.PARAMETER InstallDir
    Where FreeClaw lives. Defaults to the directory this script is in when
    that is an install, and to %LOCALAPPDATA%\FreeClaw otherwise.

.PARAMETER Purge
    Also delete .env, Flask\static, logs and browser-profiles. Irreversible.

.PARAMETER Yes
    Do not ask for confirmation.

.NOTES
    Piped through `iex` there is nowhere to put parameters, so each one also
    reads an environment variable: FREECLAW_DIR, FREECLAW_PURGE, FREECLAW_YES.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [switch]$Purge,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

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
$Purge      = Env-Flag $Purge "FREECLAW_PURGE"
$Yes        = Env-Flag $Yes   "FREECLAW_YES"

if (-not $InstallDir) {
    # A copy of this script ships inside every install, so if it is sitting in
    # one, that is the install to remove - whatever directory it happens to be
    # in. Someone who installed to D:\Apps\FreeClaw should not have to say so.
    # $PSScriptRoot is empty when the script is piped through `iex`, which is
    # what falls through to the default location.
    #
    # One explicit marker, written by install.ps1 and gitignored, rather than
    # guessing from the contents.
    #
    # Guessing does not work here. An install *is* a clone of the repo, so it
    # has .git and install.ps1 exactly like a developer's checkout does - and
    # uninstalling a checkout, deleting src\ and Flask\ out of somebody's
    # working tree, would be a spectacular own goal. Guessing from the app
    # files fails the other way too: after a non-purge uninstall they are gone,
    # which is precisely the moment someone re-runs this with -Purge.
    #
    # .freeclaw-install survives a non-purge uninstall and is removed only by
    # a purge, so it is true exactly when this directory is an install with
    # anything left in it.
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot ".freeclaw-install"))) {
        $InstallDir = $PSScriptRoot
    } else {
        $InstallDir = Join-Path $env:LOCALAPPDATA "FreeClaw"
    }
}

function Step($text) { Write-Host ""; Write-Host "  $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "     $text" -ForegroundColor DarkGray }
function Ok($text)   { Write-Host "     $text" -ForegroundColor Green }
function Die($text)  { Write-Host ""; Write-Host "  x $text" -ForegroundColor Red; Write-Host ""; exit 1 }

Write-Host ""
Write-Host "  FreeClaw - uninstall" -ForegroundColor Yellow
Write-Host ""

if (-not (Test-Path $InstallDir)) { Die "Nothing installed at $InstallDir." }

Info "install:  $InstallDir"
if ($Purge) {
    Write-Host "     data:     WILL BE DELETED (-Purge)" -ForegroundColor Red
} else {
    Info "data:     kept (.env, chats, logs, browser logins)"
}

if (-not $Yes) {
    Write-Host ""
    $answer = Read-Host "  Continue? [y/N]"
    if ($answer -ne "y" -and $answer -ne "Y") { Write-Host "  Cancelled."; Write-Host ""; exit 0 }
}

# -- stop it --------------------------------------------------
$pidFile = Join-Path $InstallDir "freeclaw.pid"
if (Test-Path $pidFile) {
    Step "Stopping FreeClaw"
    $thePid = (Get-Content $pidFile -First 1).Trim()
    if ($thePid -match '^\d+$') {
        & taskkill.exe /F /T /PID $thePid /FI "IMAGENAME eq pythonw.exe" 2>&1 | Out-Null
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Ok "stopped"
}
# Belt and braces: a crash can leave the pid file behind or missing entirely.
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

# -- shortcut / autostart / PATH ------------------------------
Step "Removing shortcuts and settings"

# Only ours. A shortcut called FreeClaw.lnk may belong to a *different*
# FreeClaw - someone can perfectly well have the .exe install in
# %LOCALAPPDATA%\FreeClaw and a second one elsewhere - and deleting it because
# the name matched would break an install this script was never asked to touch.
# So every one of these is checked against $InstallDir before it goes.
function Remove-OurShortcut($path, $label) {
    if (-not (Test-Path $path)) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $target = $shell.CreateShortcut($path).TargetPath
    } catch {
        Info "left $label alone (couldn't read where it points)"
        return
    }
    if ($target -and $target.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item $path -Force
        Ok $label
    } else {
        Info "left $label alone - it points at another install"
    }
}

Remove-OurShortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "FreeClaw.lnk") "Start Menu shortcut"
Remove-OurShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "FreeClaw.lnk") "desktop shortcut"

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$run = (Get-ItemProperty -Path $runKey -Name "FreeClaw" -ErrorAction SilentlyContinue).FreeClaw
if ($run) {
    if ($run -like ("*" + $InstallDir + "*")) {
        Remove-ItemProperty -Path $runKey -Name "FreeClaw" -ErrorAction SilentlyContinue
        Ok "autostart entry"
    } else {
        Info "left the autostart entry alone - it points at another install"
    }
}

# Our one entry, matched whole, and only rewritten if it is actually there.
# Anything else here would be editing the user's PATH on their behalf.
$binDir = (Join-Path $InstallDir "bin").TrimEnd('\').ToLower()
$key = "HKCU:\Environment"
$current = (Get-ItemProperty -Path $key -Name Path -ErrorAction SilentlyContinue).Path
if ($current) {
    $kept = @()
    $found = $false
    foreach ($entry in ($current -split ';')) {
        if ($entry.Trim() -eq "") { continue }
        if ($entry.Trim().TrimEnd('\').ToLower() -eq $binDir) { $found = $true; continue }
        $kept += $entry
    }
    if ($found) {
        Set-ItemProperty -Path $key -Name Path -Value ($kept -join ';') -Type ExpandString
        Ok "PATH entry"
    }
}

# -- files ----------------------------------------------------
Step "Removing files"
# uninstall.ps1 is kept alongside the data it left behind: without it there is
# nothing in the directory that can finish the job, and `-Purge` later would
# mean fetching the script again. It is also, on a normal run, the file this
# very process is executing - deleting that mid-run aborts the script, which is
# how an uninstall that had actually worked came to exit 255 with no summary.
$userData = @(".env", "Flask\static", "logs", "browser-profiles", "uninstall.ps1",
              ".freeclaw-install")

if ($Purge) {
    # Everything except this script first, so a genuine problem (a file still
    # open, a permissions issue) is reported rather than hidden behind the
    # self-delete below.
    foreach ($item in Get-ChildItem $InstallDir -Force) {
        if ($item.Name -eq "uninstall.ps1") { continue }   # deletes itself last
        Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    $left = @(Get-ChildItem $InstallDir -Force | Where-Object { $_.Name -ne "uninstall.ps1" })
    if ($left.Count) {
        Die ("Couldn't fully delete $InstallDir - " +
             "$($left.Count) item(s) left, something still has a file open. " +
             "Try again after signing out.")
    }
    # A running script cannot delete itself. Hand the last step to a detached
    # cmd that waits for this process to exit first.
    $cmd = 'timeout /t 2 /nobreak >nul & rmdir /s /q "' + $InstallDir + '"'
    Start-Process cmd.exe -ArgumentList '/c', $cmd -WindowStyle Hidden | Out-Null
    Ok "everything removed"
} else {
    foreach ($item in Get-ChildItem $InstallDir -Force) {
        if ($item.Name -eq "Flask") {
            foreach ($sub in Get-ChildItem $item.FullName -Force) {
                if ($sub.Name -eq "static") { continue }
                Remove-Item $sub.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
            continue
        }
        if ($userData -contains $item.Name) { continue }
        Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    Ok "program files removed"
}

Write-Host ""
Write-Host "  ----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  FreeClaw has been uninstalled." -ForegroundColor Green
if (-not $Purge) {
    Write-Host ""
    Write-Host "  Your data is still in:" -ForegroundColor DarkGray
    Write-Host "    $InstallDir"
    Write-Host ""
    Write-Host "  Delete that folder to remove it, or finish the job with:" -ForegroundColor DarkGray
    Write-Host "    & `"$InstallDir\uninstall.ps1`" -Purge"
}
Write-Host ""
