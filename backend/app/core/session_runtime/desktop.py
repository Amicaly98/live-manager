"""desktop.py —— **桌面适配点**（来源提交 8bd2fb6 的共享运行链之外的全部桌面差异）。

共享链（``session/`` 与 ``session_runtime/`` 的其余模块）按服务端提交原样迁入；本
模块只承载"桌面产品与服务器产品的三处真实差别"，便于以后按差异重新同步：

1. **启动只提示恢复，不自动开播**（``manual_resume_only``）。服务端重启后按持久
   Goal 自动续播；桌面必须停在"可恢复"状态等用户点继续。落实点在 **Bootstrap 可
   派发 Start 之前**（``production.ApplicationOwner._pre_bootstrap``），不是先开播
   再补一次 Stop。
2. **首次领取账号目录之前完成旧资料接管**（``prepare_account_target``）。桌面升级
   前的 ``live_state.json`` / ``live_tasks.db`` 就躺在数据目录里，必须先经既有的
   显式迁移工具（``account_migrate.run_migration``：预检 → 一致性候选 → 转换核对 →
   原子发布）搬进 ``accounts/<uid>/``，**才能**让 ``open_account_binding`` 建库。
   顺序反了就会先落一个空目标库，之后再无合法覆盖路径。接管前先按既有归属能力
   （库登记 / 有效状态身份 / 旧档房间号）判定这份资料属于谁：**只有与本账号一致
   才自动接管**，确证属于别人就保留原件照常新开，归属不明或读不出则拒绝激活并
   说明问题 —— 本次扫码 UID 本身不是历史授权。规则见
   :func:`prepare_account_target` 的说明。
3. **设置页是视频目录/FFmpeg 路径的权威**。桌面用 Electron 目录选择器写的
   ``settings.json:video_path`` 必须真正进入选片与进程创建，而不是只让诊断接口读到。

这里**不新增**第二套业务规则：迁移、归属、锁、关闭预算、账本全部复用共享实现，
本模块只做"调用哪一段既有能力"和"怎么解释桌面旧文件"。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: 桌面旧版的用户停止意图文件（``live_stop`` 时代写入，1.x 沿用）。
STOP_INTENT_FILE = 'stop_intent.json'

#: 迁移**明确拒绝**但它并不说明"这份资料属于别人"的判据码。遇到这些时宁可**不激活**
#: 这个账号，也不能让用户看到一个空任务库却以为旧资料已经接管。
#:
#: 反过来说，``owner_mismatch`` / ``state_owner_mismatch`` / ``room_mismatch`` /
#: ``run_mismatch`` 是"旧资料**确证**属于另一个账号"：原件保留、如实报告，本次登录的
#: 新账号照常拿到自己的空库，旧资料等原账号再扫码时再迁移。
_FOREIGN_OWNERSHIP_REASONS = frozenset((
    'owner_mismatch', 'state_owner_mismatch', 'room_mismatch', 'run_mismatch',
))

#: 不构成失败、可以照常继续激活的迁移结论。
_CONTINUE_STATUSES = frozenset(('published', 'already_done', 'ready'))
_CONTINUE_REASONS = frozenset(('source_empty', 'target_exists'))

#: :func:`_ownership_verdict` 的四种结论。
_OWNERSHIP_BOUND = 'bound'            # 有可核对的线索，且与本账号一致
_OWNERSHIP_FOREIGN = 'foreign'        # 有可核对的线索，且**确证**属于别的账号
_OWNERSHIP_UNKNOWN = 'unknown'        # 既没有归属登记、也没有场次身份
_OWNERSHIP_UNREADABLE = 'unreadable'  # 归属登记**在**却读不出（≠ 没有登记）


# ==================== 视频目录（设置页是权威） ====================

def video_base_from_settings(settings: Any) -> Optional[Path]:
    """从设置里解释出**生效的**视频根目录；拿不到可信值时返回 None。

    非空才算设置过：桌面 ``settings.json`` 里该字段为空串时沿用 ``VIDEO_BASE_PATH``
    的兜底，不把"没设置过"解释成"根目录是当前工作目录"。
    """
    value = getattr(settings, 'video_path', None)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value).expanduser()
    except (OSError, ValueError, TypeError):
        return None


# ==================== 桌面停止意图 ====================

def read_stop_intent(data_dir: Path) -> Dict[str, Any]:
    """只读解释桌面旧版的 ``stop_intent.json``。

    返回 ``{'status', 'detail', 'facts'}``，``status`` ∈
    ``missing`` / ``ok`` / ``unreadable``。

    **读不出不等于不存在**：文件在而解析失败一律报 ``unreadable``，调用方必须按
    "存在停止意图"处理（保守方向），不得报成"没有停止意图"。
    """
    path = Path(data_dir) / STOP_INTENT_FILE
    try:
        if not path.is_file():
            return {'status': 'missing', 'detail': '', 'facts': {}}
    except OSError as exc:
        return {'status': 'unreadable', 'detail': f'无法确认停止意图文件：{exc}',
                'facts': {}}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'status': 'unreadable',
                'detail': f'停止意图文件存在但读不出：{exc}', 'facts': {}}
    if not isinstance(document, dict):
        return {'status': 'unreadable',
                'detail': '停止意图文件存在但不是对象', 'facts': {}}
    facts: Dict[str, Any] = {}
    for key in ('stopped_at', 'zone', 'epoch'):
        if key in document:
            facts[key] = document[key]
    return {'status': 'ok', 'detail': '', 'facts': facts}


def stop_marker_content(intent: Dict[str, Any]) -> str:
    """桌面"重开不自动开播"这条产品规则落成共享链认识的**外部停止标记**的内容。

    为什么是标记而不是另写一套：``.no_auto_resume`` 在既有阶段 2 解释器里正是
    "外部停止指令"（``session.migrate._stop_intent``：标记 → ``STOP_USER``，并注记
    "停止输入只产生 Goal=stopped，不自动 Start"）。它只表达一件事——**这一份旧
    资料不是"正在跑、重启后应当自动续播"的**——由既有解释器在**候选转换**时落实，
    不新建第二套停止机制、不写第二个标记文件。

    三种来源分别有话说：

    - ``ok``：用户升级前确实点过停止（``stop_intent.json`` 在）；
    - ``missing``：桌面产品规则本身（重开只提示恢复，不自动开播）；
    - ``unreadable``：文件在但读不出 —— 按**存在**停止意图处理（宁可多一次"启动不
      自动开播"，也不能把读失败当成"用户没停过"）。

    注意本函数**只产出内容**，落点由调用方决定：它只作为候选目录里的附加文件进入
    :class:`~.account_migrate.MigrationRequest`，**绝不**先写到来源目录（复核 R4）。
    """
    return json.dumps({
        'source': 'desktop-restart-policy',
        'intent': intent.get('status'),
        'facts': dict(intent.get('facts') or {}),
    }, ensure_ascii=False, indent=2)


def stop_intent_note(intent: Dict[str, Any]) -> str:
    """本次落下的停止意图**为什么**落下（只用于报告，不是第二份权威）。"""
    if intent.get('status') == 'unreadable':
        return f'桌面停止意图读不出（{intent.get("detail")}）：已按"存在停止意图"带上'
    if intent.get('status') == 'ok':
        return '桌面停止意图已带上（升级前人工停止继续有效）'
    return '桌面产品规则：重开应用只提示恢复、不自动开播'


# ==================== 旧资料接管 ====================

def _ownership_verdict(evidence: Dict[str, Any], *, uid: int,
                       room_id: int) -> Tuple[str, str]:
    """自动接管前对旧资料归属的**唯一**判定（只读，不改字节）。

    依据取自既有能力，不为此另造账号系统：

    1. **库归属登记**（``tasks_meta.account_owner_uid``）——最硬的证据；
    2. **有效状态快照冻结的运行身份**（``run.uid``）——迁移产物自带；
    3. **旧格式 ``LiveState`` 的房间号**——旧档没有 UID，但它记下的房间是登录时
       从平台实际取得的事实，两者一致才构成本账号的归属依据（复核 R4 的同一份
       预检判据）。

    读不出（登记在却解析不出 / 库形状不可读）**不等于**没有登记，单独成一类：它
    必须走保守出口，不能被"未标注"吞掉。

    四种结论**都只是"能不能自动接管"的判据**，没有一种是"先领取再说"：没有任何
    线索时返回 ``unknown``，调用方据此拒绝自动接管。
    """
    db_state = evidence.get('db_owner_state')
    if db_state in ('corrupt', 'unreadable'):
        return _OWNERSHIP_UNREADABLE, f'旧任务库归属登记/形状读不出（{db_state}）'
    db_uid = evidence.get('db_owner_uid')
    if db_state == 'ok' and db_uid is not None:
        if int(db_uid) != int(uid):
            return _OWNERSHIP_FOREIGN, f'旧任务库登记属于 UID {int(db_uid)}'
        return _OWNERSHIP_BOUND, f'旧任务库登记属于本账号 UID {int(uid)}'

    run_uid = int(evidence.get('state_run_uid') or 0)
    if run_uid:
        if run_uid != int(uid):
            return _OWNERSHIP_FOREIGN, f'旧场次快照的运行账号是 UID {run_uid}'
        return _OWNERSHIP_BOUND, f'旧场次快照的运行账号是本账号 UID {run_uid}'

    legacy = evidence.get('state_legacy') or {}
    room = int(evidence.get('state_run_room') or 0) or int(legacy.get('room_id') or 0)
    if room:
        if int(room_id) <= 0:
            return _OWNERSHIP_UNKNOWN, f'旧资料记录了房间 {room}，但本次登录没有可核对的房间'
        if room != int(room_id):
            return _OWNERSHIP_FOREIGN, (f'旧资料记录的房间是 {room}，'
                                        f'与本次登录的房间 {int(room_id)} 不符')
        return _OWNERSHIP_BOUND, f'旧资料记录的房间 {room} 与本次登录一致'
    return _OWNERSHIP_UNKNOWN, '旧资料既没有归属登记、也没有场次身份'


def prepare_account_target(data_dir: Path, account_root: Path, account: Any,
                           ) -> Optional[str]:
    """首次领取账号目录**之前**的旧资料接管；返回拒绝原因（None 表示可以继续）。

    调用点固定在 ``ApplicationOwner.activate_account`` 里、``open_account_binding``
    的**前面** —— 这是"不许先登录生成空目标再覆盖迁移"的物理保证。

    顺序完全沿用既有显式迁移工具：只读预检 → 领取 UID 占用 → 一致性候选副本 →
    转换/归属核对 → 原子发布。本函数只负责三件桌面专属的事：

    - **什么时候跑**：只在目标账号目录还不存在、且数据目录里确实有旧资料时；
    - **先证明归属**（复核 R2）：这次扫码拿到的 UID **不是**历史权威，因此它不能
      自己构成迁移授权。可核对的归属线索与本账号一致才自动接管；线索**确证**属于
      别人 → 原件保留、本次照常新开；线索读不出、或**根本没有线索** → 明确拒绝
      激活并说明归属问题（既不发布、也不建空库把问题藏起来），留给既有的**显式
      迁移工具**按用户已确定的映射处理。产品现在没有"归属确认"入口，因此这里不做
      任何形式的自动认领；
    - **带上桌面旧字段**：把 ``stop_intent.json`` 转换成共享链认识的停止标记，作为
      **候选目录**里的附加文件进入迁移（``MigrationRequest.extra_auxiliary``），
      因此它与账号资料同步发布，且**拒绝迁移时来源目录一个字节都不动**（复核 R4）；
    - **失败怎么定性**：区分"确证属于别人"（保留原件、如实报告、本次账号照常新开）
      与"读不出/归属不明/转换不过"（**拒绝激活**，绝不留下一个会被当成接管成功的
      空目标）。
    """
    from .account_migrate import (
        MigrationRequest, preflight, read_source_evidence, run_migration,
    )
    from .accounts import account_paths
    from .production import NO_AUTO_RESUME_MARKER

    source = Path(data_dir)
    root = Path(account_root)
    uid = int(getattr(account, 'uid', 0) or 0)
    if uid <= 0:
        return '账号上下文缺少 UID：旧资料无法确定归属，保留原件不接管'

    paths = account_paths(root, uid)
    if paths.account_dir.exists():
        # 该账号目录已经存在（上一次接管、或上一次登录已经建过库）：这里**不动**
        # 它。已有可信库不会被覆盖，这是既有迁移工具的预检判据，不在这里重写。
        return None

    try:
        evidence = read_source_evidence(source)
    except Exception as exc:                                  # noqa: BLE001
        return (f'旧资料只读预检失败（{type(exc).__name__}: {exc}）：'
                '不接管、不建空库，保留原件')
    if not evidence.get('db_present') and not evidence.get('state_present'):
        # 真的没有旧资料（已在只读预检里确认过，不是"读不出"）：按首次启动继续。
        return None

    room_id = int(getattr(account, 'room_id', 0) or 0) or None
    verdict, detail = _ownership_verdict(evidence, uid=uid,
                                         room_id=int(room_id or 0))
    request = MigrationRequest(uid=uid, source_dir=source, shared_root=root,
                               room_id=room_id)

    if verdict == _OWNERSHIP_UNREADABLE:
        return (f'旧资料归属读不出（{detail}）：不接管、不建空库，保留原件')
    if verdict == _OWNERSHIP_UNKNOWN:
        # **归属不明不自动发**：本次扫码 UID 不是历史权威，也就不是这张迁移授权
        # （交办 §7 / 计划 §"可信 UID 无法确定时保留原件并说明归属问题，不把旧任务
        # 自动交给下一个扫码账号"）。这里既不发布、也不建空库把问题藏起来 —— 明确
        # 拒绝激活并说明归属问题，旧资料留给既有的**显式迁移工具**按用户已确定的
        # 映射处理。
        logger.warning('旧资料归属不明（%s）：不自动接管，来源保留待显式迁移', detail)
        _remember({
            'status': 'refused', 'reason': 'ownership_unknown', 'detail': detail,
            'uid': uid, 'target_dir': str(paths.account_dir),
        })
        return (f'旧资料归属不明（{detail}）：本次不自动接管、不建空库，'
                '原件与运行库均未改动；请用既有显式迁移工具按已确定的账号映射处理')
    if verdict == _OWNERSHIP_FOREIGN:
        # 仍走一次**只读**预检，把既有判据码（room_mismatch / owner_mismatch /
        # state_owner_mismatch）如实登记进接管事实；预检不写任何字节、不建目标目录。
        report = preflight(request).to_dict()
        logger.warning('旧资料确证属于另一个账号（%s）：原件保留，本次不接管', detail)
        _remember(report)
        return None

    intent = read_stop_intent(source)
    extra: Dict[str, str] = {}
    marker_note = ''
    if evidence.get('state_present'):
        # 有**旧场次资料**要接管时才带上标记。它进的是**候选目录**：随账号资料一起
        # 发布，来源目录（以及它自己的 .no_auto_resume，若有）一个字节都不动。
        extra[NO_AUTO_RESUME_MARKER] = stop_marker_content(intent)
        marker_note = stop_intent_note(intent)
        logger.info('桌面停止意图：%s', marker_note)

    request = MigrationRequest(uid=uid, source_dir=source, shared_root=root,
                               room_id=room_id, extra_auxiliary=extra)
    report = run_migration(request).to_dict()
    logger.info('桌面旧资料接管：%s（%s）', report.get('status'),
                report.get('reason') or report.get('detail'))
    _remember(report, marker_note)

    status = str(report.get('status') or '')
    reason = str(report.get('reason') or '')
    if status in _CONTINUE_STATUSES:
        return None
    if reason in _FOREIGN_OWNERSHIP_REASONS:
        # 旧资料**确证**属于另一个账号：保留原件，本次账号按新账号继续。
        logger.warning('旧资料属于另一个账号（%s）：原件保留，本次不接管',
                       report.get('detail') or reason)
        return None
    if reason in _CONTINUE_REASONS:
        return None
    return (f'旧资料接管未通过（{reason or status}）：'
            f'{report.get("detail") or "未产出可信账号资料"}；'
            '原件与运行库均未改动，未建立空目标')


#: 最近一次接管的可核对事实（仅供 health/报告读取；不是第二份业务权威）。
LAST_TAKEOVER: Dict[str, Any] = {}


def _remember(report: Dict[str, Any], marker_note: str = '') -> None:
    LAST_TAKEOVER.clear()
    LAST_TAKEOVER.update({
        'status': report.get('status'),
        'reason': report.get('reason'),
        'detail': report.get('detail'),
        'uid': report.get('uid'),
        'target_dir': report.get('target_dir'),
        'source_fingerprint': report.get('source_fingerprint'),
        'source_counts': (report.get('facts') or {}).get('source_counts'),
        'steps': list(report.get('steps') or []),
        'stop_intent': marker_note,
    })


def current_takeover_facts() -> Dict[str, Any]:
    return dict(LAST_TAKEOVER)


# ==================== 默认 owner（桌面策略） ====================

def build_desktop_owner(*, data_dir: Path):
    """构造桌面默认 owner：共享组装 + 桌面策略，不另写一条运行链。"""
    from .production import build_application_owner

    owner = build_application_owner(data_dir=data_dir)
    #: 桌面产品语义：重启只提示恢复，不自动开播（见模块头）。
    owner.manual_resume_only = True
    #: 首次领取账号目录前先做旧资料接管（见 prepare_account_target）。
    owner.account_preparer = (
        lambda account: prepare_account_target(
            Path(data_dir), owner.account_root, account))
    return owner
