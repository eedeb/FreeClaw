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

; The addtopath task edits HKCU\Environment. This is what makes Setup and the
; uninstaller broadcast WM_SETTINGCHANGE afterwards, so an Explorer already
; running picks the new PATH up and consoles opened from it can find `freeclaw`
; without a sign-out.
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startupicon"; Description: "Start FreeClaw when I sign in"; GroupDescription: "Startup"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts"; Flags: unchecked
Name: "addtopath"; Description: "Add the ""freeclaw"" command to my PATH"; GroupDescription: "Command line"

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

; Put the freeclaw CLI shim on PATH — {app}\bin, never {app}, so this doesn't
; also hand the user's shell python.exe and tray.py.
;
; expandsz, not string: HKCU\Environment\Path routinely contains %USERPROFILE%
; and friends, and rewriting it as a plain REG_SZ would freeze those into
; literal text. preservestringtype keeps an existing REG_SZ Path a REG_SZ
; rather than silently changing its type.
;
; NO uninsdeletevalue here. On this key that flag would delete the user's
; entire Path on uninstall; RemoveFromPath() in [Code] takes out our one entry
; instead.
;
; The value is built by PathWithBin rather than written as "{olddata};{app}\bin"
; so that an empty or trailing-semicolon Path can't produce an empty entry —
; Windows reads one of those as "the current directory", which is a search path
; nobody asked to add.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{code:PathWithBin}"; Tasks: addtopath; \
    Flags: preservestringtype; Check: NeedsAddPath(ExpandConstant('{app}\bin'))

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
const
  EnvironmentKey = 'Environment';

var
  PasswordPage: TInputQueryWizardPage;
  TelemetryCheck: TNewCheckBox;

{ ── PATH ─────────────────────────────────────────────────────

  HKCU\Environment\Path is the user's own PATH and nothing else in the install
  is anywhere near as easy to damage: one careless write and every command on
  their machine stops resolving. So everything below reads it first, compares
  whole entries, and writes only when there is something to change. }

function GetUserPath(var Path: String): Boolean;
begin
  Result := RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Path);
  if not Result then
    Path := '';
end;

{ One PATH entry reduced to a comparable form: uppercased, trimmed, and with
  any trailing backslash removed, wrapped in the separator on both sides. The
  wrapping is what stops a substring test matching a *longer* directory that
  merely begins the same way — without it, an existing ...\FreeClaw\bin2 would
  read as ...\FreeClaw\bin already being present. }
function PathEntryKey(Dir: String): String;
var
  D: String;
begin
  D := Trim(Dir);
  while (Length(D) > 0) and (D[Length(D)] = '\') do
    D := Copy(D, 1, Length(D) - 1);
  Result := ';' + Uppercase(D) + ';';
end;

{ Used as a Check: on the [Registry] entry, so reinstalling doesn't append a
  second copy of the same directory every time. }
function NeedsAddPath(Dir: String): Boolean;
var
  Path, Haystack: String;
begin
  GetUserPath(Path);
  Haystack := ';' + Uppercase(Trim(Path)) + ';';
  { Fold "...\bin\;" to "...\bin;" so an entry written with a trailing
    separator still matches the key built above. }
  StringChangeEx(Haystack, '\;', ';', True);
  Result := Pos(PathEntryKey(Dir), Haystack) = 0;
end;

{ The Path to write: what is there now, plus our bin directory. Any trailing
  separators are dropped first so the result never contains an empty entry. }
function PathWithBin(Param: String): String;
var
  Path: String;
begin
  GetUserPath(Path);
  Path := Trim(Path);
  while (Length(Path) > 0) and (Path[Length(Path)] = ';') do
    Path := Copy(Path, 1, Length(Path) - 1);
  if Path = '' then
    Result := ExpandConstant('{app}\bin')
  else
    Result := Path + ';' + ExpandConstant('{app}\bin');
end;

{ Take our own directory back out on uninstall, entry by entry. The [Registry]
  section can't do this: uninsdeletevalue on Environment\Path would delete the
  whole PATH. }
procedure RemoveFromPath(Dir: String);
var
  Path, Rebuilt, Item, Key: String;
  P: Integer;
  Found: Boolean;
begin
  if not GetUserPath(Path) then
    Exit;

  Key := PathEntryKey(Dir);
  Rebuilt := '';
  Found := False;
  Path := Path + ';';
  while Length(Path) > 0 do
  begin
    P := Pos(';', Path);
    if P = 0 then
      P := Length(Path) + 1;
    Item := Copy(Path, 1, P - 1);
    Path := Copy(Path, P + 1, Length(Path));
    if Trim(Item) = '' then
      Continue;
    if PathEntryKey(Item) = Key then
    begin
      Found := True;
      Continue;
    end;
    if Rebuilt <> '' then
      Rebuilt := Rebuilt + ';';
    Rebuilt := Rebuilt + Item;
  end;

  { Nothing of ours in there — someone removed it by hand, or the task was
    never ticked. Leave the value completely alone rather than rewriting it
    with our own idea of spacing and separators. }
  if not Found then
    Exit;

  { When we do rewrite, empty entries are gone: the loop skips them. That is a
    real if small change to something we did not put there, and it is the right
    way round — Windows resolves an empty PATH entry against the current
    directory, so a stray ";;" or trailing ";" is a search path nobody asked
    for. Everything else keeps its text, order and %VARIABLE% references. }

  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Rebuilt);
end;

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

// The password for this install: what was typed on the password page, or for
// an unattended install the value of /PASSWORD= on the command line.
function ChosenPassword(): String;
begin
  if WizardSilent() then
    Result := ExpandConstant('{param:PASSWORD|}')
  else
    Result := PasswordPage.Values[0];
end;

// Telemetry likewise. An unattended install that says nothing gets no ping —
// this is opt-in, and a script that never saw the checkbox hasn't opted in.
function ChosenTelemetry(): Boolean;
begin
  if WizardSilent() then
    Result := ExpandConstant('{param:TELEMETRY|0}') = '1'
  else
    Result := TelemetryCheck.Checked;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  { Nothing to validate without a wizard — and returning False here in silent
    mode wedges Setup in a loop it can never leave, re-asking a question that
    has no page to answer it on. The silent path is checked in
    PrepareToInstall instead. }
  if WizardSilent() then
    Exit;
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

  { A silent install has no page to type a password on, so it has to be handed
    one. Refusing here rather than carrying on is the point: write_env.py would
    otherwise seed an empty FC_PASSWORD, and the web UI would come up on the
    network with no password at all. Returning a message aborts Setup with a
    non-zero exit code, which is what a script driving this needs to see. }
  if WizardSilent() and (not HasExistingPassword()) and
     (Length(ChosenPassword()) < 4) then
  begin
    Result := 'A silent install needs a password of at least 4 characters for ' +
              'the FreeClaw web interface. Re-run Setup with ' +
              '/PASSWORD=yourpassword, or run it without /SILENT to be asked ' +
              'for one.';
    Exit;
  end;

  StopFreeClaw();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopFreeClaw();
    RemoveFromPath(ExpandConstant('{app}\bin'));
  end;
end;

// Hand the password to write_env.py through a file in {tmp} rather than on the
// command line: arguments are readable by anything that can enumerate
// processes, and this one guards the whole web UI. write_env.py deletes it
// after reading, and Inno wipes {tmp} at the end regardless.
//
// Deliberately // and not the { } the rest of this section uses: Pascal
// comments do not nest, so the } of {tmp} would close the comment and turn the
// rest of the sentence into code. Any comment naming an Inno constant has to
// use // for that reason.
function EnvSeedArgs(Param: String): String;
var
  PasswordFile: String;
begin
  Result := '"' + ExpandConstant('{app}\windows\write_env.py') + '"' +
            ' --env "' + ExpandConstant('{app}\.env') + '"';

  if not HasExistingPassword() then
  begin
    PasswordFile := ExpandConstant('{tmp}\fc-password');
    if SaveStringToFile(PasswordFile, ChosenPassword(), False) then
      Result := Result + ' --password-file "' + PasswordFile + '"';
    if ChosenTelemetry() then
      Result := Result + ' --telemetry 1'
    else
      Result := Result + ' --telemetry 0';
  end;
end;
