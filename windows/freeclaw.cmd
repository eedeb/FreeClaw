@echo off
rem FreeClaw CLI launcher - the Windows counterpart of the /usr/local/bin/freeclaw
rem shim that install.sh writes. The installer puts this in <install>\bin and adds
rem that one directory to the user PATH, so `freeclaw` works from any console.
rem
rem Two things it has to get right:
rem
rem   * The interpreter. It must be the bundled one - that is where FreeClaw's
rem     dependencies live, and the machine may well have no other Python at all.
rem     %~dp0 is this file's own directory (with a trailing backslash), so the
rem     path holds wherever the user chose to install.
rem
rem   * The working directory. src\cli.py writes into Flask\static\ through paths
rem     built from the install root, and a user running `freeclaw` from their
rem     Documents folder must not end up pointing those at Documents. pushd moves
rem     there and popd puts the user back where they started.
rem
rem %* forwards arguments (the CLI takes a username) with their quoting intact.
rem
rem Keep this file ASCII with CRLF line endings. cmd.exe reads a batch file in the
rem OEM codepage and mis-parses bare LF, which turns these rem lines into commands
rem it then tries to run. .gitattributes pins the line endings; please keep both.

setlocal
pushd "%~dp0.."
"%~dp0..\python\python.exe" -m src.cli %*
set FREECLAW_EXIT=%ERRORLEVEL%
popd
endlocal & exit /b %FREECLAW_EXIT%
