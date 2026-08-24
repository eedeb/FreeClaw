; FreeClaw — Windows installer (Inno Setup 6.3+)
;
; Built by windows\build.ps1, which stages a self-contained tree (app files
; plus a bundled Python) and passes its location in. Don't run this directly:
; without /DStageDir there is nothing to package.
;
;   ISCC /DAppVersion=1.2.3 /DStageDir=...\build\stage /DOutputDir=...\dist installer.iss
;
; Two decisions shape everything below.
;
; Per-user, not per-machine. PrivilegesRequired=lowest means no UAC prompt at
; any point, which matters on locked-down work machines. It also puts the
; install under %LOCALAPPDATA%, a directory Windows already ACLs to this user
; — that is what actually protects browser-profiles\, which holds live signed-in
; browser sessions. os.chmod(0o700) in src/browser_profiles.py does nothing on
; Windows beyond toggling the read-only bit, so the install location is the
; protection.
;
; Uninstall keeps user data. Chats, uploads, context.md, logs, saved browser
; logins and .env are all created at runtime, so Inno never tracks them and
; never removes them. The one shipped conversation ("Setup Wizard") is marked
; uninsneveruninstall for the same reason — by then it may have been added to.

#ifndef AppVersion
  #error Pass /DAppVersion=<version>
#endif
#ifndef StageDir
  #error Pass /DStageDir=<path to build\stage>
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName    "FreeClaw"
#define AppPublisher "FreeClaw"
#define AppURL     "https://freeclaw.eedeb.dev"
#define TrayScript "{app}\windows\tray.py"

[Setup]
; Never change AppId — it is how Windows recognises an upgrade rather than a
; parallel second install.
AppId={{B246128E-59AC-43BB-B402-247B62F51C21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVersion}

DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

OutputDir={#OutputDir}
OutputBaseFilename=FreeClaw-Setup-{#AppVersion}
SetupIconFile={#SourcePath}\freeclaw.ico
UninstallDisplayIcon={app}\windows\freeclaw.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LicenseFile={#StageDir}\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startupicon"; Description: "Start FreeClaw when I sign in"; GroupDescription: "Startup"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts"; Flags: unchecked

[Files]
; The one shipped conversation. onlyifdoesntexist + uninsneveruninstall so a
; reinstall doesn't overwrite it and an uninstall doesn't take it away.
Source: "{#StageDir}\Flask\static\Setup Wizard\*"; DestDir: "{app}\Flask\static\Setup Wizard"; \
    Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall

; Everything else. Flask\static\ is excluded here and handled above, so a
; reinstall can never walk over the user's own chat folders.
Source: "{#StageDir}\*"; DestDir: "{app}"; \
    Excludes: "Flask\static\*"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Dirs]
; Created empty so the first write doesn't have to. logs\ in particular is
; where the tray reports a failed start, which is exactly when nothing else works.
Name: "{app}\logs"
Name: "{app}\Flask\static"

[Icons]
; The shortcut runs pythonw.exe (no console window) with tray.py. There is no
; launcher .exe to sign or maintain — the icon comes from IconFilename.
Name: "{group}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; \
    Parameters: """{#TrayScript}"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\windows\freeclaw.ico"; Comment: "Run FreeClaw in the notification area"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; \
    Parameters: """{#TrayScript}"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\windows\freeclaw.ico"; Tasks: desktopicon

[Registry]
; Must match the command windows\tray.py writes from its own "Start with
; Windows" menu item, or the menu checkbox and the installer task would
; disagree about whether autostart is on.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "FreeClaw"; \
    ValueData: """{app}\python\pythonw.exe"" ""{#TrayScript}"""; \
    Flags: uninsdeletevalue; Tasks: startupicon

[Run]
; Seed .env with the password, a fresh SECRET_KEY and the telemetry choice.
; The script merges rather than overwrites, so on an upgrade this is a no-op
; and the user's providers and MCP servers survive untouched.
Filename: "{app}\python\python.exe"; Parameters: "{code:EnvSeedArgs}"; \
    WorkingDir: "{app}"; Flags: runhidden waituntilterminated; \
    StatusMsg: "Writing configuration..."

Filename: "{app}\python\pythonw.exe"; Parameters: """{#TrayScript}"""; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; \
    Description: "Start FreeClaw now"

[Code]
var
  PasswordPage: TInputQueryWizardPage;
  TelemetryCheck: TNewCheckBox;

{ True when .env already has a login password — i.e. this is an upgrade or a
  reinstall over an existing install. The password page is pointless then:
  write_env.py would refuse to overwrite the value anyway, so asking for one
  would collect a password that silently does nothing. }
function HasExistingPassword(): Boolean;
var
  Lines: TArrayOfString;
  I: Integer;
  Line: String;
begin
  Result := False;
  if not FileExists(ExpandConstant('{app}\.env')) then
    Exit;
  if not LoadStringsFromFile(ExpandConstant('{app}\.env'), Lines) then
    Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Trim(Lines[I]);
    if (Pos('FC_PASSWORD=', Line) = 1) and (Length(Line) > Length('FC_PASSWORD=')) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

procedure InitializeWizard();
begin
  PasswordPage := CreateInputQueryPage(wpSelectTasks,
    'FreeClaw password',
    'Choose the password for the FreeClaw web interface.',
    'The chat UI sits behind a login screen so it is safe to open on your ' +
    'home network. This is not an API key — you add AI providers later, from ' +
    'Settings inside the app.');
  PasswordPage.Add('Password:', True);
  PasswordPage.Add('Confirm password:', True);

  TelemetryCheck := TNewCheckBox.Create(PasswordPage);
  TelemetryCheck.Parent := PasswordPage.Surface;
  TelemetryCheck.Top := PasswordPage.Edits[1].Top + PasswordPage.Edits[1].Height +
                        ScaleY(18);
  TelemetryCheck.Left := PasswordPage.Edits[1].Left;
  TelemetryCheck.Width := PasswordPage.SurfaceWidth;
  TelemetryCheck.Height := ScaleY(17);
  TelemetryCheck.Caption := 'Send one anonymous install ping (no messages, no keys)';
  TelemetryCheck.Checked := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = PasswordPage.ID) and HasExistingPassword();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> PasswordPage.ID then
    Exit;

  if Length(PasswordPage.Values[0]) < 4 then
  begin
    MsgBox('Please choose a password of at least 4 characters.', mbError, MB_OK);
    Result := False;
  end
  else if PasswordPage.Values[0] <> PasswordPage.Values[1] then
  begin
    MsgBox('The two passwords do not match.', mbError, MB_OK);
    Result := False;
  end;
end;

{ Stop a running FreeClaw so its files aren't locked when we overwrite or
  delete them.

  The PID comes from freeclaw.pid, which windows\tray.py writes at startup and
  removes on a clean exit (see write_pid_file there). Killing by PID rather
  than by matching process paths keeps this free of quoting hazards: an
  install path is user-controlled, and a username containing an apostrophe is
  enough on its own to break a PowerShell command built by concatenation.

  /T takes the supervised server down with the tray. The IMAGENAME filter is
  the safety net — a stale PID file left behind by a crash could in principle
  name a recycled PID belonging to something else, and the filter means the
  worst case is failing to kill, not killing the wrong thing.

  A missing or stale file just makes taskkill fail, which is harmless: the
  install proceeds, and Windows reports a locked file if one really is. }
procedure StopFreeClaw();
var
  PidFile: String;
  Lines: TArrayOfString;
  Pid: String;
  ResultCode: Integer;
begin
  PidFile := ExpandConstant('{app}\freeclaw.pid');
  if not FileExists(PidFile) then
    Exit;
  if not LoadStringsFromFile(PidFile, Lines) then
    Exit;
  if GetArrayLength(Lines) = 0 then
    Exit;

  Pid := Trim(Lines[0]);
  if Pid = '' then
    Exit;

  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /T /PID ' + Pid + ' /FI "IMAGENAME eq pythonw.exe"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  DeleteFile(PidFile);
  { Windows releases the file handles asynchronously once the process dies. }
  Sleep(1500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopFreeClaw();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopFreeClaw();
end;

{ Hand the password to write_env.py through a file in {tmp} rather than on the
  command line: arguments are readable by anything that can enumerate
  processes, and this one guards the whole web UI. write_env.py deletes it
  after reading, and Inno wipes {tmp} at the end regardless. }
function EnvSeedArgs(Param: String): String;
var
  PasswordFile: String;
begin
  Result := '"' + ExpandConstant('{app}\windows\write_env.py') + '"' +
            ' --env "' + ExpandConstant('{app}\.env') + '"';

  if not HasExistingPassword() then
  begin
    PasswordFile := ExpandConstant('{tmp}\fc-password');
    if SaveStringToFile(PasswordFile, PasswordPage.Values[0], False) then
      Result := Result + ' --password-file "' + PasswordFile + '"';
    if TelemetryCheck.Checked then
      Result := Result + ' --telemetry 1'
    else
      Result := Result + ' --telemetry 0';
  end;
end;
