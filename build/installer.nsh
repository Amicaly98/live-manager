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
; 做法：在 `.onInit`（早于 `uninstallOldVersion`）把已知的旧数据文件**复制**到
; 一个不会被卸载清掉的位置：
;
;     %APPDATA%\<包名>\legacy-rescue
;
; 应用侧（electron/main.ts 的 getLegacyDataDirs）把同一路径作为既有
; `--legacy-data-dir` 入口的一个候选交给后端，走原本就有的
; 「只读预检 → 一致性候选 → 转换核对 → 原子发布」链，**不新增迁移体系**。
; 这里只负责"在删除之前把原件复制出去"，不解释内容、不判定归属。
;
; 复制清单 = 后端 data_migration._MIGRATED_FILES ∪ {live_tasks.db 的 -wal/-shm}。
; 与该清单保持一致：多了会搬走后端不认的文件，少了会丢已经能接管的资料。
; （-wal/-shm 必须一起走：后端用 SQLite backup API 做一致性快照，只拿主库会
; 丢掉还在 WAL 里的已提交内容。）
;
; 边界：
; - 只在**检测到旧数据文件**时才建目标目录，纯新装不产生任何残留；
; - 目标已存在时同名文件按来源覆盖（同一份来源重复安装是幂等的，且第二次升级时
;   安装根目录里已经没有旧文件，已有的保全结果不会被清掉）；
; - 采用**当前用户**的 %APPDATA%（与 Electron 的 userData 同级）。本产品是
;   按用户安装（RequestExecutionLevel user / installMode CurrentUser），这也是
;   应用真正读取数据的那个用户上下文。
; ============================================================================

!include "LogicLib.nsh"

!define LEGACY_RESCUE_SUBDIR "legacy-rescue"

; 这两个变量只在**安装器**那一趟编译里声明。卸载器那一趟（BUILD_UNINSTALLER）
; 不会插入 customInit，声明了就成了"声明但从未使用"，而 electron-builder 把
; NSIS 的 warning 当错误——所以这里显式按编译目标分开。
!ifndef BUILD_UNINSTALLER
Var legacyRescueDir
Var legacyRescueSource
!endif

; 复制清单里的单个文件（存在才复制）。
!macro legacyRescueCopyOne FILENAME
  ${If} ${FileExists} "$legacyRescueSource\${FILENAME}"
    CopyFiles /SILENT "$legacyRescueSource\${FILENAME}" "$legacyRescueDir\${FILENAME}"
  ${EndIf}
!macroend

; 从 $legacyRescueSource 保全一份；只认"这确实是一份 1.0.x 数据目录"的四个特征文件。
!macro legacyRescueFromSource
  ${If} $legacyRescueSource != ""
    ${If} ${FileExists} "$legacyRescueSource\live_tasks.db"
    ${OrIf} ${FileExists} "$legacyRescueSource\live_state.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\bili_cookies.json"
    ${OrIf} ${FileExists} "$legacyRescueSource\settings.json"
      CreateDirectory "$legacyRescueDir"
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
!macroend

; 入口：installer.nsi 的 .onInit 在 initMultiUser（据此定出 $INSTDIR）之后调用。
; 此时 $INSTDIR 就是**既有安装**所在目录（或 /D 指定的目标），尚未被卸载器清空。
!macro customInit
  StrCpy $legacyRescueDir "$APPDATA\${APP_PACKAGE_NAME}\${LEGACY_RESCUE_SUBDIR}"

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
!macroend
