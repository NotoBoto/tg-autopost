; Установщик TG Автопостера (Inno Setup 6). Собирается из build.py:
;   ISCC /DAppVersion=0.1.0 /DSourceDir=dist\TG Autopost /Odist installer.iss
; Ставится без прав администратора в %LOCALAPPDATA%\Programs. Данные (настройки, сессии)
; программа хранит в %APPDATA%\TG Autopost — обновление их не трогает.
; Автообновление запускает установщик так:  /SILENT /UPDATED=1 /RUN=0|1 /TRAY=0|1

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\TG Autopost"
#endif
#define AppExe "TG Autopost.exe"
#define AppTitle "TG Автопостер"

[Setup]
; AppId не менять — по нему установщик узнаёт уже установленную программу
AppId={{07FE1118-03D6-44F2-BCE3-E0D2056BA228}
AppName={#AppTitle}
AppVersion={#AppVersion}
AppVerName={#AppTitle} {#AppVersion}
AppPublisher=NotoBoto
AppPublisherURL=https://github.com/NotoBoto/tg-autopost
AppSupportURL=https://github.com/NotoBoto/tg-autopost/issues
AppUpdatesURL=https://github.com/NotoBoto/tg-autopost/releases
DefaultDirName={localappdata}\Programs\TG Autopost
DefaultGroupName={#AppTitle}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=TG-Autopost-{#AppVersion}-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppTitle}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Программа при закрытии окна уходит в трей — закрываем её принудительно
CloseApplications=force
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "autostart"; Description: "Запускать вместе с Windows (сразу в трей и через 30 секунд начинать работу)"; GroupDescription: "Дополнительно:"; Flags: unchecked

[InstallDelete]
; Библиотеки прошлой версии — чтобы после обновления не осталось лишних файлов
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppTitle}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppTitle}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Та же запись, что ставит переключатель «Запускать вместе с Windows» в программе.
; При автообновлении не трогаем: пользователь мог выключить автозапуск в программе.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TG Autopost"; ValueData: """{app}\{#AppExe}"" --autostart"; Tasks: autostart; Check: not IsUpdate
; При удалении убираем автозапуск, даже если его включили в самой программе
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "TG Autopost"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppTitle}"; Flags: nowait postinstall skipifsilent
; После автообновления — запустить новую версию и продолжить работу
Filename: "{app}\{#AppExe}"; Parameters: "--updated --run={param:RUN|0} --tray={param:TRAY|0}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""{#AppExe}"""; Flags: runhidden; RunOnceId: "CloseApp"

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATED|0}') = '1';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{userappdata}\TG Autopost');
  if DirExists(DataDir) and not UninstallSilent then
    if MsgBox('Удалить также настройки, промпты и сессии Telegram?' + #13#10 + DataDir + #13#10#13#10 +
              'Нет — оставить: при повторной установке не придётся заново всё настраивать и входить в Telegram.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir, True, True, True);
end;
