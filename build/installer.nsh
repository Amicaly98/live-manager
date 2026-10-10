; ============================================================================
; build/installer.nsh —— electron-builder 自定义 NSIS 片段（由 nsis.include 自动拾取）
;
; 桌面移植第三阶段 A 包：**覆盖升级时的旧资料保全**。
;
; 背景（实测复现：deliveries/desktop-port-phase3-20261010/upgrade-repro.log）：
; 1.0.x 的 Electron 用 spawn 拉起后端且未设 cwd，运行时数据（live_tasks.db /
; live_state.json / bili_cookies.json / settings.json …）就落在**安装根目录**。
; electron-builder 的安装器在同 appId 覆盖安装时，先在 installSection.nsh 里静默
; 跑**旧版**卸载器，而旧卸载器会清空 $INSTDIR —— 于是这些资料在"升级成功"的同时
; 消失，且没有任何提示。
;
; ---------------------------------------------------------------------------
; 这一版的责任（独立复核 A1/A2 之后的定稿）：
;
;   **在旧卸载清空 $INSTDIR 之前，取得一份"安全且完整"的保全结果；
;     取不到就明确中止这次升级。**
;
; 两个承重点：
;
; 1. **保全会发生在旧应用停写之后**（复核 A2）。
;    入口挂在模板的 `customCheckAppRunning` 上：installSection.nsh 先
;    `!insertmacro CHECK_APP_RUNNING`（这里会按模板既有流程结束正在运行的本应用），
;    紧接着就是 `uninstallOldVersion`。挂在它后面 = "模板既有的关闭流程刚刚跑完、
;    旧卸载还没开始"这一刻，而不是 .onInit（那时应用可能还在写库）。
;    光靠"应该已经关了"还不够：复制前会用**共享模式 0** 打开旧主库做一次停写确证，
;    只有这一刻没有任何进程持有它，主库+WAL+SHM 这一族的副本才对得上某个真实存在的
;    时刻。确证不了就中止（复核 A2 的受控反例：源库只出现过 (0,0)→(1,0)→(1,1)，
;    按"先主库后 WAL"逐个复制能拼出从未存在过的 (0,1)，而且 integrity_check 还是 ok）。
;
; 2. **任何一份需要保全的文件没被完整保存，就在旧卸载之前中止**（复核 A1）。
;    每次复制后都核对目标与源的大小一致；不一致/没落地/目录建不出来 → 中止。
;    只打印一条日志然后继续，等于把资料交给旧卸载器。
;
; 应用侧（electron/main.ts 的 getLegacyDataDirs）把同一路径作为既有
; `--legacy-data-dir` 入口的一个候选交给后端，走原本就有的
; 「只读预检 → 一致性候选 → 转换核对 → 原子发布」链，**不新增迁移体系**。
; 这里只负责"在删除之前把原件复制出去"，不解释内容、不判定归属。
;
; 复制清单 = 后端 data_migration._MIGRATED_FILES ∪ {live_tasks.db 的 -wal/-shm}。
; 与该清单保持一致：多了会搬走后端不认的文件，少了会丢已经能接管的资料。
;
; 边界：
; - 只在**检测到旧数据文件**时才建目标目录，纯新装不产生任何残留；
; - 目标已存在时同名文件按来源覆盖（同一份来源重复安装是幂等的，且第二次升级时
;   安装根目录里已经没有旧文件，已有的保全结果不会被清掉）；
; - 中止时，若保全目录是**这一次**新建的，把它整个删掉——半份副本留在那里，
;   下次启动会被当成一份完整的旧资料接管；
; - 采用**当前用户**的 %APPDATA%（与 Electron 的 userData 同级）。本产品是
;   按用户安装（RequestExecutionLevel user / installMode CurrentUser），这也是
;   应用真正读取数据的那个用户上下文。
; ============================================================================

!include "LogicLib.nsh"

!define LEGACY_RESCUE_SUBDIR "legacy-rescue"

; ---------------------------------------------------------------------------
; 本文件定义了 `customCheckAppRunning`，模板因此**不再**自行
; `!include "getProcessInfo.nsh"` / `Var pid`（见 allowOnlyOneInstallerInstance.nsh
; 第 5 行起的 !ifmacrondef 分支）。这两样由本文件补齐，否则 `_CHECK_APP_RUNNING`
; 展开时会找不到 `${GetProcessInfo}` 与 `$pid`。
;
; 判据用 APP_GUID：它是 electron-builder 通过命令行 -D 传给 makensis 的，
; 只会在真实构建里存在。这样本文件仍然可以被"直接 include 的独立小探针"使用
; （那种用法下不会插入 customCheckAppRunning，也就不需要这两样）。
; ---------------------------------------------------------------------------
!ifdef APP_GUID
  !include "getProcessInfo.nsh"
  Var pid
!endif

; 这三个变量只在**安装器**那一趟编译里用得到。卸载器那一趟（BUILD_UNINSTALLER）
; 用不上，声明了就成了"声明但从未使用"，而 electron-builder 把 NSIS 的 warning
; 当错误（warning 6001 → Error: warning treated as error）。
!ifndef BUILD_UNINSTALLER
Var legacyRescueDir
Var legacyRescueSource
Var legacyRescueError
Var legacyRescueDirCreated
!endif

; ---------------------------------------------------------------------------
; 停写确证：用共享模式 0 打开旧主库。
;   打开成功 ⇒ 这一刻没有任何进程持有它（写者已停）；
;   打开失败 ⇒ 读不出"停写"这件事，按未知处理，**不能**开始复制。
; 主库不存在（只可能是 live_state.json 这类单文件资料）就没什么要确证的。
; 做成 Function 是为了能有循环/重试标号：同一个宏会被插入多次，带标号会重名。
; ---------------------------------------------------------------------------
!ifndef BUILD_UNINSTALLER
Function legacyRescueProbeQuiescent
  ${If} ${FileExists} "$legacyRescueSource\live_tasks.db"
    StrCpy $R3 0
    legacy_rescue_probe_loop:
      System::Call 'kernel32::CreateFileW(w "$legacyRescueSource\live_tasks.db", i 0x80000000, i 0, i 0, i 3, i 0x80, i 0) i .R4'
      ${If} $R4 != -1
        System::Call 'kernel32::CloseHandle(i R4)'
        Return
      ${EndIf}
      IntOp $R3 $R3 + 1
      ${If} $R3 < 6
        Sleep 1000
        Goto legacy_rescue_probe_loop
      ${EndIf}
      StrCpy $legacyRescueError "live_tasks.db 仍被其他进程占用：无法确证旧程序已停写"
  ${EndIf}
FunctionEnd
!endif

; 复制清单里的单个文件：存在才复制，**并核对目标与源的大小一致**。
; 任何一步不成立都写进 $legacyRescueError（非空 = 这次保全不可信）。
!macro legacyRescueCopyOne FILENAME
  ${If} ${FileExists} "$legacyRescueSource\${FILENAME}"
    ClearErrors
    CopyFiles /SILENT "$legacyRescueSource\${FILENAME}" "$legacyRescueDir\${FILENAME}"
    FileOpen $R0 "$legacyRescueSource\${FILENAME}" r
    ${If} $R0 == ""
      StrCpy $legacyRescueError "${FILENAME}: 源文件读不出来"
    ${Else}
      FileSeek $R0 0 END $R1
      FileClose $R0
      FileOpen $R0 "$legacyRescueDir\${FILENAME}" r
      ${If} $R0 == ""
        StrCpy $legacyRescueError "${FILENAME}: 没有保全到"
      ${Else}
        FileSeek $R0 0 END $R2
        FileClose $R0
        ${If} $R1 != $R2
          StrCpy $legacyRescueError "${FILENAME}: 保全不完整（源 $R1 字节 / 副本 $R2 字节）"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; 从 $legacyRescueSource 保全一份；只认"这确实是一份 1.0.x 数据目录"的四个特征文件。
; 顺序是：**先确证停写，再复制**——不是"先拼三个文件，回头再想办法对齐"。
!macro legacyRescueFromSource
  ${If} $legacyRescueSource != ""
    ${If} ${FileExists} "$legacyRescueSource\live_tasks.db"
    ${OrIf} ${FileExists} "$legacyRescueSource\live_state.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\bili_cookies.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\settings.json"
      DetailPrint "legacy-rescue: 检查 $legacyRescueSource"
      Call legacyRescueProbeQuiescent
      ${If} $legacyRescueError == ""
        ${IfNot} ${FileExists} "$legacyRescueDir"
          CreateDirectory "$legacyRescueDir"
          StrCpy $legacyRescueDirCreated "1"
        ${EndIf}
        SetOverwrite on
        !insertmacro legacyRescueCopyOne "live_state.json"
        !insertmacro legacyRescueCopyOne "live_tasks.db"
        !insertmacro legacyRescueCopyOne "live_tasks.db-wal"
        !insertmacro legacyRescueCopyOne "live_tasks.db-shm"
        !insertmacro legacyRescueCopyOne "live_tasks.xlsx"
        !insertmacro legacyRescueCopyOne "bili_cookies.json"
        !insertmacro legacyRescueCopyOne "settings.json"
        !insertmacro legacyRescueCopyOne "stop_intent.json"
        !insertmacro legacyRescueCopyOne "rtmp_cache.json"
        !insertmacro legacyRescueCopyOne "bili_areas_full.json"
        !insertmacro legacyRescueCopyOne "task_manager_last_run.json"
        DetailPrint "legacy-rescue: $legacyRescueSource -> $legacyRescueDir"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; 保全没做到就**中止这次安装**——此刻旧卸载还没跑，原件仍在安装目录里。
!macro legacyRescueAbortIfFailed
  ${If} $legacyRescueError != ""
    DetailPrint "legacy-rescue 中止：$legacyRescueError"
    ${If} $legacyRescueDirCreated == "1"
      ; 半份副本比没有副本更危险：应用会把它当成一份完整的旧资料去接管。
      RMDir /r "$legacyRescueDir"
    ${EndIf}
    SetErrorLevel 3
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "旧版本的任务数据无法在本机安全保全，本次升级已在删除旧文件之前中止。$\r$\n$\r$\n原因：$legacyRescueError$\r$\n$\r$\n请完全退出「直播控制系统」（含托盘与后台进程）后重试。"
    ${EndIf}
    Quit
  ${EndIf}
!macroend

; 入口：installSection.nsh 的 CHECK_APP_RUNNING 会在**旧卸载之前**调用
; customCheckAppRunning，此刻 $INSTDIR 还是既有安装所在目录（尚未被清空）。
!macro legacyRescueRun
  StrCpy $legacyRescueDir "$APPDATA\${APP_PACKAGE_NAME}\${LEGACY_RESCUE_SUBDIR}"
  StrCpy $legacyRescueError ""
  StrCpy $legacyRescueDirCreated "0"

  ; 来源 1：本次安装目标（静默 /D= 或注册表登记的既有安装位置）
  StrCpy $legacyRescueSource "$INSTDIR"
  !insertmacro legacyRescueFromSource

  ; 来源 2：注册表登记的既有安装位置（用户这次把新版本装到别处时，旧资料在那里）
  ReadRegStr $0 "HKEY_CURRENT_USER" "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $0 != ""
  ${AndIf} $0 != $INSTDIR
    StrCpy $legacyRescueSource "$0"
    !insertmacro legacyRescueFromSource
  ${EndIf}

  ReadRegStr $0 "HKEY_LOCAL_MACHINE" "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $0 != ""
  ${AndIf} $0 != $INSTDIR
  ${AndIf} $0 != $legacyRescueSource
    StrCpy $legacyRescueSource "$0"
    !insertmacro legacyRescueFromSource
  ${EndIf}

  StrCpy $legacyRescueSource ""
  !insertmacro legacyRescueAbortIfFailed
!macroend

; ---------------------------------------------------------------------------
; 挂点。模板的 CHECK_APP_RUNNING 会优先用这个宏（存在即取代它自己的实现），
; 所以这里先原样插入 `_CHECK_APP_RUNNING`（既有关闭流程，行为不变），
; 再在"应用已经关掉、旧卸载马上要跑"的这一刻做保全。
; 卸载器那一趟也走这个宏（un.checkAppRunning），但保全只属于安装：卸载时既没有
; 旧卸载要躲，也没有必要动 $INSTDIR。
; ---------------------------------------------------------------------------
!macro customCheckAppRunning
  !insertmacro _CHECK_APP_RUNNING
  !ifndef BUILD_UNINSTALLER
    !insertmacro legacyRescueRun
  !endif
!macroend
