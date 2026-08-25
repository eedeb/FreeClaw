@echo off
rem Start the FreeClaw tray app (the supervisor that runs the server).
rem
rem The Start Menu shortcut install.ps1 creates points straight at pythonw.exe,
rem because the installer knows its own absolute path. This shim exists so that
rem `freeclaw-tray` also works as a command, from wherever the install ended up:
rem the path is worked out at run time from %~dp0.
rem
rem `start ""` launches pythonw and returns immediately, so this script exits
rem instead of sitting in the process tree for the whole session. pythonw.exe
rem rather than python.exe is what keeps the tray console-free.
rem
rem Keep this file ASCII with CRLF line endings - see freeclaw.cmd.

start "" "%~dp0..\python\pythonw.exe" "%~dp0..\windows\tray.py" %*
