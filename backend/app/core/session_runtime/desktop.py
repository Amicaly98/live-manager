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
   顺序反了就会先落一个空目标库，之后再无合法覆盖路径。
3. **设置页是视频目录/FFmpeg 路径的权威**。桌面用 Electron 目录选择器写的
   ``settings.json:video_path`` 必须真正进入选片与进程创建，而不是只让诊断接口读到。

这里**不新增**第二套业务规则：迁移、归属、锁、关闭预算、账本全部复用共享实现，
本模块只做"调用哪一段既有能力"和"怎么解释桌面旧文件"。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

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


def materialize_stop_marker(data_dir: Path, intent: Dict[str, Any]) -> str:
    """把桌面"重开不自动开播"这条产品规则落成共享链认识的**外部停止标记**。

    为什么是标记而不是另写一套：``.no_auto_resume`` 在既有阶段 2 解释器里正是
    "外部停止指令"（``session.migrate._stop_intent``：标记 → ``STOP_USER``，并注记
    "停止输入只产生 Goal=stopped，不自动 Start"）。转换只表达一件事——**这一份旧
    资料不是"正在跑、重启后应当自动续播"的**——由既有解释器与既有 Bootstrap 前
    落实门处理，不新建第二套停止机制、不写第二个标记文件。

    三种来源分别有话说：

    - ``ok``：用户升级前确实点过停止（``stop_intent.json`` 在）；
    - ``missing``：桌面产品规则本身（重开只提示恢复，不自动开播）；
    - ``unreadable``：文件在但读不出 —— 按**存在**停止意图处理（宁可多一次"启动不
      自动开播"，也不能把读失败当成"用户没停过"）。
    """
    from .production import NO_AUTO_RESUME_MARKER

    marker = Path(data_dir) / NO_AUTO_RESUME_MARKER
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            'source': 'desktop-restart-policy',
            'intent': intent.get('status'),
            'facts': dict(intent.get('facts') or {}),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as exc:
        # 落标记失败必须如实上报：这条事实没有进入账号资料，不能假装已带上。
        return f'桌面停止意图未能落成标记（{exc}）'
    if intent.get('status') == 'unreadable':
        return f'桌面停止意图读不出（{intent.get("detail")}）：已按"存在停止意图"带上'
    if intent.get('status') == 'ok':
        return '桌面停止意图已带上（升级前人工停止继续有效）'
    return '桌面产品规则：重开应用只提示恢复、不自动开播'


# ==================== 旧资料接管 ====================

def prepare_account_target(data_dir: Path, account_root: Path, account: Any,
                           ) -> Optional[str]:
    """首次领取账号目录**之前**的旧资料接管；返回拒绝原因（None 表示可以继续）。

    调用点固定在 ``ApplicationOwner.activate_account`` 里、``open_account_binding``
    的**前面** —— 这是"不许先登录生成空目标再覆盖迁移"的物理保证。

    顺序完全沿用既有显式迁移工具：只读预检 → 领取 UID 占用 → 一致性候选副本 →
    转换/归属核对 → 原子发布。本函数只负责三件桌面专属的事：

    - **什么时候跑**：只在目标账号目录还不存在、且数据目录里确实有旧资料时；
    - **带上桌面旧字段**：把 ``stop_intent.json`` 先落成共享链认识的停止标记，随
      既有辅助文件一起进入候选（因此它与账号资料同步发布，不是发布后再补写）；
    - **失败怎么定性**：区分"确证属于别人"（保留原件、如实报告、本次账号照常新开）
      与"读不出/转换不过"（**拒绝激活**，绝不留下一个会被当成接管成功的空目标）。
    """
    from .account_migrate import (
        MigrationRequest, preflight, read_source_evidence, run_migration,
    )
    from .accounts import account_paths

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

    intent = read_stop_intent(source)
    marker_note = ''
    if evidence.get('state_present'):
        # 有**旧场次资料**要接管时才落标记：桌面产品规则是"重开应用只提示恢复、
        # 不自动开播"，因此这一份资料不是"正在跑、重启后应当自动续播"的。标记随
        # 既有辅助文件一起进入候选并随账号目录发布，**不是**发布后再补写。
        marker_note = materialize_stop_marker(source, intent)
        if marker_note:
            logger.info('桌面停止意图：%s', marker_note)

    room_id = int(getattr(account, 'room_id', 0) or 0) or None
    request = MigrationRequest(uid=uid, source_dir=source, shared_root=root,
                               room_id=room_id)
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
