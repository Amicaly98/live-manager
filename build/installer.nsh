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
; 这一版的责任（第三轮独立复核 R1/R2/R3 之后的定稿）：
;
;   **在旧卸载清空 $INSTDIR 之前，取得一份"安全且完整"的保全结果；
;     取不到就明确中止这次升级，而且绝不改动已经存在的那一份保全。**
;
; 四个承重点：
;
; 1. **保全会发生在旧应用停写之后**（第二轮复核 A2）。
;    入口挂在模板的 `customCheckAppRunning` 上：installSection.nsh 先
;    `!insertmacro CHECK_APP_RUNNING`（这里会按模板既有流程结束正在运行的本应用），
;    紧接着就是 `uninstallOldVersion`。挂在它后面 = "模板既有的关闭流程刚刚跑完、
;    旧卸载还没开始"这一刻，而不是 .onInit（那时应用可能还在写库）。
;
; 2. **排写保护覆盖整个复制与核验窗口**（本轮复核 R3）。
;    先按模板之外最严的方式确证停写（共享模式 0 打开旧主库，重试若干次），随后取一个
;    **只读、允许其他读者、拒绝任何写者**的句柄（GENERIC_READ + FILE_SHARE_READ），
;    **一直握到本次复制与逐份核验结束才释放**（成功/失败出口都释放）。
;    只在复制前拿一次又立刻关掉，挡不住复制窗口里重新开库的写者：复核的受控交错里
;    源库只出现过 (0,0)→(1,0)→(1,1)，按"先主库后 WAL"逐个复制却能拼出源库从未出现过的
;    (0,1)，而且 integrity_check 还是 ok；之后再调 backup API 也救不回来。
;
; 3. **复制结果要真的是"本次复制"的结果**（本轮复核 R1）。
;    每次复制都先进一个**本次新建的干净候选目录**，并**消费 CopyFiles 自己的错误标志**，
;    再逐份核对源/副本大小。候选里不可能存在"上一轮的旧副本"，所以
;    "目标存在且大小相同"这件事在这里只可能来自本次复制——不再用已有目标的长度
;    冒充本次操作成功。
;
; 4. **已有保全要么原样不动，要么被一份完整的新保全整体替换**（本轮复核 R2）。
;    全部文件复制并核验成功之后，才做一次切换：已有保全**移到一边**（不是删除）→
;    候选换上去 → 删掉移开的那份。任何一步不成就整体回退并中止，已有保全原样不动。
;    失败时既不留混合目录（四份新版 + 一份旧库），也不删掉已有的完整保全；
;    半份副本比没有副本更危险——应用会把它当成一份完整的旧资料接管。
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
; - 只在**检测到旧数据文件**时才建候选目录，纯新装不产生任何残留；
; - 候选目录用固定后缀（`legacy-rescue-new`）、被移开的旧保全用 `legacy-rescue-old`；
;   两者都不是应用读取的路径，应用只读 `legacy-rescue`；
; - 采用**当前用户**的 %APPDATA%（与 Electron 的 userData 同级）。本产品是
;   按用户安装（RequestExecutionLevel user / installMode CurrentUser），这也是
;   应用真正读取数据的那个用户上下文。
; ============================================================================

!include "LogicLib.nsh"

!define LEGACY_RESCUE_SUBDIR "legacy-rescue"
!define LEGACY_RESCUE_CANDIDATE_SUFFIX "-new"
!define LEGACY_RESCUE_PREVIOUS_SUFFIX "-old"

; 真实构建里这些名字都由 electron-builder 用 -D 传给 makensis；这里给注册表键一条
; 兜底，好让"直接 include 本文件"的独立小探针也能编译（命中不了任何真实键）。
!ifndef INSTALL_REGISTRY_KEY
  !define INSTALL_REGISTRY_KEY "Software\Phase3RescueHarness\NoSuchKey"
!endif

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

; 这些变量只在**安装器**那一趟编译里用得到。卸载器那一趟（BUILD_UNINSTALLER）
; 用不上，声明了就成了"声明但从未使用"，而 electron-builder 把 NSIS 的 warning
; 当错误（warning 6001 → Error: warning treated as error）。
!ifndef BUILD_UNINSTALLER
Var legacyRescueDir
Var legacyRescueCandidate
Var legacyRescuePrevious
Var legacyRescueSource
Var legacyRescueError
Var legacyRescueDirCreated
Var legacyRescueHasData
Var legacyRescueGuardHeld
!endif

; ---------------------------------------------------------------------------
; "这确实像一份 1.0.x 数据目录吗" —— 四个特征文件任一存在即算。
; 输入 $R7（目录），输出 $legacyRescueHasData（"1"/"0"）。
; ---------------------------------------------------------------------------
!ifndef BUILD_UNINSTALLER
Function legacyRescueLooksLikeOldData
  StrCpy $legacyRescueHasData "0"
  ${If} ${FileExists} "$R7\live_tasks.db"
  ${OrIf} ${FileExists} "$R7\live_state.json"
  ${OrIf} ${FileExists} "$R7\bili_cookies.json"
  ${OrIf} ${FileExists} "$R7\settings.json"
    StrCpy $legacyRescueHasData "1"
  ${EndIf}
FunctionEnd

; 选来源：先看本次安装目标，再看注册表登记的既有安装位置（用户这次把新版本装到
; 别处时，旧资料留在登记的位置）。只认第一个**确实像旧数据目录**的来源。
Function legacyRescuePickSource
  StrCpy $legacyRescueSource ""
  StrCpy $R7 "$INSTDIR"
  Call legacyRescueLooksLikeOldData
  ${If} $legacyRescueHasData == "1"
    StrCpy $legacyRescueSource "$R7"
    Return
  ${EndIf}

  ReadRegStr $R7 "HKEY_CURRENT_USER" "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $R7 != ""
  ${AndIf} $R7 != "$INSTDIR"
    Call legacyRescueLooksLikeOldData
    ${If} $legacyRescueHasData == "1"
      StrCpy $legacyRescueSource "$R7"
      Return
    ${EndIf}
  ${EndIf}

  ReadRegStr $R7 "HKEY_LOCAL_MACHINE" "${INSTALL_REGISTRY_KEY}" "InstallLocation"
  ${If} $R7 != ""
  ${AndIf} $R7 != "$INSTDIR"
    Call legacyRescueLooksLikeOldData
    ${If} $legacyRescueHasData == "1"
      StrCpy $legacyRescueSource "$R7"
    ${EndIf}
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------------------
; 停写确证 + 排写保护。
;   共享模式 0 打开成功 ⇒ 这一刻没有任何进程持有它（写者已停）；
;   打开失败 ⇒ 读不出"停写"这件事，按未知处理，重试若干次后中止。
;   紧接着取的第二个句柄是**只读 + 共享只读**：别的读者还能读（所以稍后的
;   CopyFiles 读得到源），但任何想拿到写权限的进程都会被拒 —— 这就是覆盖
;   整个复制与核验窗口的屏障。句柄放在 $R4 里（0 = 没拿到），由
;   legacyRescueReleaseGuard 在两个出口上释放。
; 主库不存在（只可能是 live_state.json 这类单文件资料）就没什么要确证/保护的。
; 做成 Function 是为了能有循环/重试标号：同一个宏会被插入多次，带标号会重名。
; ---------------------------------------------------------------------------
Function legacyRescueTakeGuard
  StrCpy $legacyRescueGuardHeld "0"
  ${IfNot} ${FileExists} "$legacyRescueSource\live_tasks.db"
    Return
  ${EndIf}

  StrCpy $R3 0
  legacy_rescue_probe_loop:
    System::Call 'kernel32::CreateFileW(w "$legacyRescueSource\live_tasks.db", i 0x80000000, i 0, i 0, i 3, i 0x80, i 0) i .R4'
    ${If} $R4 != -1
      System::Call 'kernel32::CloseHandle(i R4)'
      Goto legacy_rescue_probe_done
    ${EndIf}
    IntOp $R3 $R3 + 1
    ${If} $R3 < 6
      Sleep 1000
      Goto legacy_rescue_probe_loop
    ${EndIf}
    StrCpy $legacyRescueError "live_tasks.db 仍被其他进程占用：无法确证旧程序已停写"
    Return

  legacy_rescue_probe_done:
  System::Call 'kernel32::CreateFileW(w "$legacyRescueSource\live_tasks.db", i 0x80000000, i 1, i 0, i 3, i 0x80, i 0) i .R4'
  ${If} $R4 == -1
    StrCpy $legacyRescueError "live_tasks.db 取不到排写保护（有进程正在写入）：拒绝在不确证停写的情况下保全"
    Return
  ${EndIf}
  StrCpy $legacyRescueGuardHeld "1"
FunctionEnd

Function legacyRescueReleaseGuard
  ${If} $legacyRescueGuardHeld == "1"
    System::Call 'kernel32::CloseHandle(i R4)'
    StrCpy $legacyRescueGuardHeld "0"
  ${EndIf}
FunctionEnd
!endif

; ---------------------------------------------------------------------------
; 候选目录：必须由**本次**建出来，且是干净的。
;   候选里不可能有"上一轮的旧副本"，因此 CopyFiles 的错误标志与随后的大小核对
;   都真的代表本次操作（这是复核 R1 那条反例的正面要求）。
; ---------------------------------------------------------------------------
!macro legacyRescuePrepareCandidate
  RMDir /r "$legacyRescueCandidate"
  Delete "$legacyRescueCandidate"
  ${If} ${FileExists} "$legacyRescueCandidate"
    StrCpy $legacyRescueError "候选保全目录清不掉（可能被占用）：$legacyRescueCandidate"
  ${Else}
    ClearErrors
    CreateDirectory "$legacyRescueCandidate"
    ${If} ${Errors}
      StrCpy $legacyRescueError "候选保全目录建不出来：$legacyRescueCandidate"
    ${Else}
      ${IfNot} ${FileExists} "$legacyRescueCandidate"
        StrCpy $legacyRescueError "候选保全目录没有落地：$legacyRescueCandidate"
      ${Else}
        StrCpy $legacyRescueDirCreated "1"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; 复制清单里的单个文件：存在才复制，**并消费 CopyFiles 的错误标志、核对大小**。
; 任何一步不成立都写进 $legacyRescueError（非空 = 这次保全不可信）。
!macro legacyRescueCopyOne FILENAME
  ${If} $legacyRescueError == ""
    ${If} ${FileExists} "$legacyRescueSource\${FILENAME}"
      ClearErrors
      CopyFiles /SILENT "$legacyRescueSource\${FILENAME}" "$legacyRescueCandidate\${FILENAME}"
      ${If} ${Errors}
        StrCpy $legacyRescueError "${FILENAME}: 复制失败（CopyFiles 报错）"
      ${Else}
        FileOpen $R0 "$legacyRescueSource\${FILENAME}" r
        ${If} $R0 == ""
          StrCpy $legacyRescueError "${FILENAME}: 源文件读不出来"
        ${Else}
          FileSeek $R0 0 END $R1
          FileClose $R0
          FileOpen $R0 "$legacyRescueCandidate\${FILENAME}" r
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
    ${EndIf}
  ${EndIf}
!macroend

; ---------------------------------------------------------------------------
; 切换：候选 → 正式路径。已有保全**移到一边**（不是删除），换不成就整体回退。
; 只在候选完整（$legacyRescueError 为空）且本次真的建过候选时才动已有保全。
; ---------------------------------------------------------------------------
!macro legacyRescueCommit
  ${If} $legacyRescueError == ""
  ${AndIf} $legacyRescueDirCreated == "1"
    RMDir /r "$legacyRescuePrevious"
    Delete "$legacyRescuePrevious"
    ${If} ${FileExists} "$legacyRescuePrevious"
      StrCpy $legacyRescueError "上一份保全的暂存位置清不掉：$legacyRescuePrevious"
    ${Else}
      ${If} ${FileExists} "$legacyRescueDir"
        ClearErrors
        Rename "$legacyRescueDir" "$legacyRescuePrevious"
        ${If} ${Errors}
          ; 已有保全被占用时移不开。这时**什么都不动**并中止：
          ; 保留它，比换上一份新副本更安全。
          StrCpy $legacyRescueError "已有保全 $legacyRescueDir 移不开（可能被占用）：拒绝在无法安全替换的情况下继续"
        ${EndIf}
      ${EndIf}
      ${If} $legacyRescueError == ""
        ClearErrors
        Rename "$legacyRescueCandidate" "$legacyRescueDir"
        ${If} ${Errors}
          ${If} ${FileExists} "$legacyRescuePrevious"
            ClearErrors
            Rename "$legacyRescuePrevious" "$legacyRescueDir"
          ${EndIf}
          StrCpy $legacyRescueError "候选保全换不进 $legacyRescueDir"
        ${Else}
          DetailPrint "legacy-rescue: $legacyRescueSource -> $legacyRescueDir"
          RMDir /r "$legacyRescuePrevious"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; ---------------------------------------------------------------------------
; 从 $legacyRescueSource 保全一份到 $legacyRescueDir；顺序是
; **先确证停写并挂上排写保护 → 再复制 → 逐份核验 → 全部成功才切换**。
; 不是"先拼三个文件，回头再想办法对齐"，也不是"直接往已有保全上覆盖"。
; 只认"这确实是一份 1.0.x 数据目录"的四个特征文件；一个都没有就什么都不做
; （纯新装、以及安装根目录里已经没有旧资料的那次重复升级，都是这条路）。
; ---------------------------------------------------------------------------
!macro legacyRescueFromSource
  ${If} $legacyRescueSource != ""
  ${AndIf} $legacyRescueError == ""
    ; 候选/暂存路径由**正式路径**推出来，调用方只需要给出 $legacyRescueDir。
    StrCpy $legacyRescueCandidate "$legacyRescueDir${LEGACY_RESCUE_CANDIDATE_SUFFIX}"
    StrCpy $legacyRescuePrevious "$legacyRescueDir${LEGACY_RESCUE_PREVIOUS_SUFFIX}"
    StrCpy $legacyRescueDirCreated "0"
    ${If} ${FileExists} "$legacyRescueSource\live_tasks.db"
    ${OrIf} ${FileExists} "$legacyRescueSource\live_state.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\bili_cookies.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\settings.json"
      DetailPrint "legacy-rescue: 检查 $legacyRescueSource"
      Call legacyRescueTakeGuard
      ${If} $legacyRescueError == ""
        !insertmacro legacyRescuePrepareCandidate
      ${EndIf}
      ${If} $legacyRescueError == ""
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
      ${EndIf}
      Call legacyRescueReleaseGuard
      !insertmacro legacyRescueCommit
    ${EndIf}
  ${EndIf}
!macroend

; 保全没做到就**中止这次安装**——此刻旧卸载还没跑，原件仍在安装目录里。
; 候选目录整个删掉（它从来不是资料来源，留半份比没有更危险）；
; **已有的那份保全一动不动**（成败都不动，只有 legacyRescueCommit 里那一次整体替换）。
!macro legacyRescueAbortIfFailed
  ${If} $legacyRescueError != ""
    DetailPrint "legacy-rescue 中止：$legacyRescueError"
    RMDir /r "$legacyRescueCandidate"
    Delete "$legacyRescueCandidate"
    ; 兜底：万一"把已有保全换回来"这一步也没成功，至少让它回到应用读取的那条路径上，
    ; 而不是以一个 -old 后缀留在那里。宁可中止升级，也不能让应用找不到已有保全。
    ${IfNot} ${FileExists} "$legacyRescueDir"
      ${If} ${FileExists} "$legacyRescuePrevious"
        Rename "$legacyRescuePrevious" "$legacyRescueDir"
      ${EndIf}
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
  StrCpy $legacyRescueCandidate "$legacyRescueDir${LEGACY_RESCUE_CANDIDATE_SUFFIX}"
  StrCpy $legacyRescuePrevious "$legacyRescueDir${LEGACY_RESCUE_PREVIOUS_SUFFIX}"
  StrCpy $legacyRescueError ""
  StrCpy $legacyRescueDirCreated "0"
  StrCpy $R4 "0"

  Call legacyRescuePickSource
  !insertmacro legacyRescueFromSource
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
