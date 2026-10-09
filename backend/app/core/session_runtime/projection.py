"""只读投影：把唯一权威快照摊成 HTTP 可返回的形状。

**唯一秒表仍在 Run.clock 里**：本模块只按同一份数据与同一个应用时钟**读出**
已确认秒数与乐观显示秒数，不新建计时权威、不写任何状态，也不修改 JSON。

- ``confirmed_seconds``：唯一可结算的业务秒数（``PlatformClock`` 的权威口径）；
- ``display_seconds``：**乐观显示**，只在已确认区间或"Start 已派出"的乐观锚点上
  外推，永不参与结算；
- ``display_advancing`` / ``display_valid_seconds``：**展示许可**与它的**剩余**
  预算。两者都由服务端时间锚定，因此同一次 GET 拿到的剩余量在页面重载或读失败
  之后只会变小，不会把同一段未知重新续满（阶段 4C）。

本模块是**纯读**：不触发 Query、不保存、不发通知、不消费人脸令牌。

阶段 4C 新增的都是**展示口径**（见 ``PHASE4C-DESIGN.md`` §2.3）：业务事实
（Step/Goal/Run/Clock/End）一个都没改，也没有第二个判定源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

#: 与 Core 的「无 Run 停等」步骤同名，供投影直接比较（不导入 Core 以免环）。
_STEP_WAIT_TASKS = 'wait_tasks'
_STEP_IDLE = 'idle'
#: Core 的「Start 已派出、等它自己的回包」步骤：**唯一**允许乐观外推的窗口。
#: 一旦离开这一步（Start 回包已到、进入 resume_query/live 观察），显示就只能停在
#: 可信检查点上——平台的"未知"回复必须把显示还回检查点（复核 R3）。
_STEP_OPEN = 'open'

#: 显示外推的**有界**预算（秒）。它是展示口径常数，不是业务规则：
#: 沿用面板既有的 120 秒展示领先上限（旧 ``stores/live.ts`` 的
#: ``OPTIMISTIC_MAX_LEAD_SEC``）。Core 的计时、结算、跨日一律不看它。
DISPLAY_LEAD_BUDGET_SECONDS = 120.0

#: ``goal.stop_reason`` 的人脸取值（与 ``session.model.STOP_FACE_VERIFY`` 同字面量，
#: 不导入 model 以免环）。
_STOP_FACE_VERIFY = 'face_verify'
_WANT_RUNNING = 'running'


def business_elapsed(clock_doc: Dict[str, Any], now_mono: Optional[float]) -> float:
    """与 ``PlatformClock.business_elapsed`` **同一公式**的只读重算。"""
    total = max(0.0, float(clock_doc.get('checkpoint_seconds') or 0.0))
    anchor = clock_doc.get('interval_start_mono')
    if clock_doc.get('projecting') and anchor is not None and now_mono is not None:
        total += max(0.0, float(now_mono) - float(anchor))
    return max(0.0, total)


def _display_view(run: Dict[str, Any], now_mono: Optional[float],
                  step: str) -> Dict[str, Any]:
    """从 Run 的检查点与**现有 Step/Clock** 算出展示口径。

    三种来源，判定只依赖 Core 已有的 Step 与 Clock 字段（不新增持久状态）：

    - ``confirmed``：``projecting`` 为真且有区间锚点 —— 平台已确认在播，外推建立在
      **已确认**区间上（``business_elapsed`` 与结算同源）。
    - ``optimistic``：仅在 ``step == 'open'`` 时 —— 这一场的 Start **真的在途**
      （已派出、还没收到它自己的回包）。它不结算任何秒数，只允许页面先走。
    - ``frozen``：其余一切 —— 显示停在可信检查点上。

    门读的是**同一 Run/Clock 的事实**，不是 ``goal.wanted``：停止意图本身不含任何
    平台事实，只保存它就退回检查点会丢掉已经确认过的展示余量（D3）。而"平台回了
    unknown"正是 ``projecting`` 被 ``suspend_projection()`` 置假的状态 ——
    此时显示回到可信检查点，``display_anchor_mono`` 仍保留供后续补接。因此乐观窗口
    必须同时要求 ``step == 'open'``：若只看锚点，未知就会被当成"仍在开播"，同一份
    未知越跑越远（复核 R3 的 5→605 秒）。

    ``valid_seconds`` 是**剩余**的展示领先预算，且满足
    ``display_seconds + valid_seconds <= confirmed_seconds + 预算``：重复 GET、
    重载、导航都只能把同一份预算**用完**，不能续满。**两条外推分支共用这一份
    预算**（复核 R3-C）：``confirmed`` 分支里服务端自己已经用掉的年龄
    （``now - interval_start_mono``）同样要从页面可用的剩余量里扣掉，否则同一锚点
    的"基础量 + 许可"会到 130/140 秒。预算耗尽时两条分支都回到"冻结"（基数仍由
    锚点给出并封顶），只有 ``frozen`` 才回落到检查点。
    """
    clock_doc = run.get('clock') or {}
    anchor = clock_doc.get('display_anchor_mono')
    projecting = bool(clock_doc.get('projecting'))
    interval = (clock_doc.get('interval_start_mono') if projecting else None)

    # 显示门读的是**同一 Run/Clock 的事实**，不是目标意图：``Goal`` 变成
    # ``stopped`` 只说明"用户想停"，它既不是 unknown 也不是 closed，不能据此撤销
    # 已经确认过的展示区间（那会让"仅保存停止意图"就把显示退回旧检查点，而平台
    # 这边还没有任何关闭证据）。反过来，时钟被 unknown（或本程序自己发出的关播
    # 动作）暂停时 ``projecting`` 为假、区间锚点不参与，显示自然回到可信检查点 ——
    # 这条**合法回退**仍然可见，不靠关掉它换绿。
    live = (run.get('end') is None
            and (interval is not None or step == _STEP_OPEN))
    frozen = {'source': 'frozen', 'advancing': False, 'valid_seconds': 0.0,
              'anchor_mono': None}
    if not live or now_mono is None:
        return frozen

    if interval is not None:
        # 已确认在播：**同一份**展示预算既管基础外推、也管页面平滑。服务端已经在这
        # 个锚点上用掉的年龄必须从页面可用的剩余量里扣掉，否则同一锚点会出现
        # ``display + valid > confirmed + 预算``（复核 R3-C：确认后第 10 秒
        # 10 + 120 = 130 秒，页面拿服务端的许可画到了 130）。
        age = max(0.0, float(now_mono) - float(interval))
        remaining = DISPLAY_LEAD_BUDGET_SECONDS - age
        if remaining <= 0:
            # 预算用尽：显示**冻结**（基数仍由锚点给出并封顶），只是不再外推；
            # 这里不回落到检查点，回落到检查点只属于 unknown（见下）。
            return {'source': 'confirmed', 'advancing': False,
                    'valid_seconds': 0.0, 'anchor_mono': float(interval)}
        return {'source': 'confirmed', 'advancing': True,
                'valid_seconds': round(remaining, 3), 'anchor_mono': float(interval)}

    if anchor is None or step != _STEP_OPEN:
        return frozen

    anchor_mono = float(anchor)
    lead = max(0.0, float(now_mono) - anchor_mono)
    remaining = DISPLAY_LEAD_BUDGET_SECONDS - lead
    if remaining <= 0:
        # 预算已用完：许可到此为止（不再"advancing=true 却零预算"）。
        return {'source': 'optimistic', 'advancing': False, 'valid_seconds': 0.0,
                'anchor_mono': anchor_mono}
    return {'source': 'optimistic', 'advancing': True,
            'valid_seconds': round(remaining, 3), 'anchor_mono': anchor_mono}


def _is_http_url(value: Any) -> bool:
    """只认**合法**的 http(s) 地址：平台错误文案绝不当成验证链接。"""
    text = str(value or '').strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
            and not parsed.username and not parsed.password)


def _face_view(goal: Dict[str, Any], account_doc: Optional[Dict[str, Any]],
               recent: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """人脸事实：**只看当前 Goal 与当前账号**，旧事实一律不展示为可继续授权。

    没有合法验证地址就如实给 ``verify_url_available=False``；``confirmable``
    与 ``ControlService.confirm`` 的接受条件同源（Goal 版本 + 账号 + 房间），
    不另立更严的门槛 —— 两边分叉就会出现"点了没反应的按钮"。
    """
    if str(goal.get('stop_reason') or '') != _STOP_FACE_VERIFY:
        return None
    if str(goal.get('wanted') or '') != 'stopped':
        return None
    revision = int(goal.get('revision') or 0)
    uid = int((account_doc or {}).get('uid') or 0)
    room_id = int((account_doc or {}).get('room_id') or 0)
    view: Dict[str, Any] = {
        'active': True,
        'goal_revision': revision,
        'uid': uid,
        'room_id': room_id,
        'zone_name': '',
        'reason': '',
        'verify_url': '',
        'verify_url_available': False,
        'confirmable': bool(uid and room_id),
        'block_reason': '' if (uid and room_id) else '当前没有已登录账号，无法确认',
    }
    for item in reversed(list(recent or [])):
        if (str(item.get('kind') or '') != 'session_open'
                or str(item.get('result') or '') != 'face_verify'):
            continue
        # 冻结事实必须与**当前**目标/账号一致：Stop、新 Goal、换账号之后
        # 这份旧事实不再属于当前等待，不能拿它展示成"可以继续授权"。
        # Goal 版本是必比项（复核 R4）：只按 uid/room 过滤时，同一账号**上一个**
        # Goal 的验证链接会被当成当前 Goal 的可用链接。
        if int(item.get('goal_revision') or 0) != revision:
            continue
        if int(item.get('uid') or 0) != uid or int(item.get('room_id') or 0) != room_id:
            continue
        reason = str(item.get('reason') or '')
        zone = str(item.get('zone_name') or '')
        break
    else:
        return view

    view['zone_name'] = zone
    view['reason'] = reason
    if _is_http_url(reason):
        view['verify_url'] = reason.strip()
        view['verify_url_available'] = True
    elif view['confirmable']:
        view['block_reason'] = '验证链接未取得，请在 B 站完成验证后回到面板确认'
    return view


def _recent_events(notifications: Any,
                   account_doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """最近后台事实（只读、有界、账号隔离）。

    账号存在时只回本账号与"未登录时期"（``uid==0``）的记录：换账号之后旧账号的
    后台事实不会出现在新会话里。
    """
    reader = getattr(notifications, 'recent_events', None)
    if not callable(reader):
        return []
    try:
        items = list(reader())
    except Exception:                                     # noqa: BLE001
        return []
    uid = int((account_doc or {}).get('uid') or 0)
    if uid:
        items = [item for item in items if int(item.get('uid') or 0) in (0, uid)]
    return items


def build_projection(snapshot: Optional[Dict[str, Any]], *,
                     clock=None, account: Any = None,
                     wait_reason: str = '',
                     notifications: Any = None) -> Dict[str, Any]:
    """把快照摊成只读投影。``snapshot is None`` = 还没有任何运行权威。"""
    now_mono: Optional[float] = None
    if clock is not None:
        try:
            now_mono = float(clock.mono())
        except Exception:                                 # noqa: BLE001
            now_mono = None

    account_doc: Optional[Dict[str, Any]] = None
    if account is not None:
        account_doc = {
            'uid': int(getattr(account, 'uid', 0)),
            'room_id': int(getattr(account, 'room_id', 0)),
            'display_name': str(getattr(account, 'display_name', '') or ''),
        }

    projection: Dict[str, Any] = {
        'has_active': False,
        'has_run': False,
        'resumable': False,
        'resume_blocked_reason': '',
        'step': '',
        'goal': None,
        'run': None,
        'current_zone': '',
        'run_id': None,
        'task_id': None,
        'business_day': '',
        'mode': '',
        'push_source': '',
        'target_seconds': 0.0,
        'confirmed_seconds': 0.0,
        'display_seconds': 0.0,
        'display_advancing': False,
        'display_valid_seconds': 0.0,
        'display_source': 'frozen',
        'duration_known': False,
        'room_id': None,
        'end': None,
        'wait_reason': wait_reason,
        'account': account_doc,
        'boot_id': str(getattr(notifications, 'boot_id', '') or ''),
        'backend_events': _recent_events(notifications, account_doc),
        'face': None,
    }
    if not isinstance(snapshot, dict):
        return projection

    goal = snapshot.get('goal') or {}
    run = snapshot.get('run')
    step = str(snapshot.get('step') or '')
    selection = goal.get('selection') or {}
    projection['step'] = step
    projection['goal'] = goal
    projection['mode'] = str(goal.get('mode') or '')
    projection['push_source'] = str(selection.get('push_source') or '')
    projection['face'] = _face_view(goal, account_doc,
                                    projection['backend_events'])

    if isinstance(run, dict):
        clock_doc = run.get('clock') or {}
        confirmed = business_elapsed(clock_doc, None)
        target = float(run.get('target_seconds') or 0.0)
        display_view = _display_view(run, now_mono, step)
        # 展示基数：可信检查点，加上**这个锚点**已经走过的领先量（`frozen` 一律
        # 不带锚点，因此仍然停在检查点上）。预算耗尽时锚点还在，于是基数停在封顶
        # 值、随 `advancing=False` 一起冻结 —— 这是"冻结显示"，不是回跳；回落到
        # 检查点只属于 unknown（`suspend_projection` 把区间锚点交给 frozen 分支）。
        displayed = confirmed
        anchor_mono = display_view.get('anchor_mono')
        if anchor_mono is not None:
            displayed = max(
                0.0, float(clock_doc.get('checkpoint_seconds') or 0.0)
            ) + max(0.0, float(now_mono or 0.0) - float(anchor_mono))
        # 基础 display_seconds 自己也受同一份展示预算约束：重复 GET / 重载 /
        # 导航都只能把同一份未知预算用完，不能靠"每次回包都给一个新基数"绕过
        # （复核 R3）。它永远不参与结算。
        displayed = min(displayed, confirmed + DISPLAY_LEAD_BUDGET_SECONDS)
        # 结束决定已形成后不再外推：结束秒数是冻结值。
        if run.get('end') is not None:
            displayed = confirmed
        projection.update({
            'run': run,
            'has_run': True,
            'run_id': run.get('id'),
            'task_id': run.get('task_id'),
            'business_day': str(run.get('business_day') or ''),
            'mode': str(run.get('mode') or projection['mode']),
            'push_source': str(run.get('push_source') or projection['push_source']),
            'current_zone': str(run.get('zone_name') or ''),
            'room_id': run.get('room_id'),
            'target_seconds': target,
            # 目标有限 ⇔ 有场次且 target_seconds > 0；"没有场次"由 has_run 表达，
            # 不再和"不限时"共用同一个布尔值（PHASE4C-DESIGN §2.3）。
            'duration_known': target > 0,
            'confirmed_seconds': round(confirmed, 3),
            'display_seconds': round(displayed, 3),
            'display_advancing': bool(display_view['advancing']),
            'display_valid_seconds': float(display_view['valid_seconds']),
            'display_source': str(display_view['source']),
            'end': run.get('end'),
        })
        projection['has_active'] = (
            run.get('end') is None and step not in (_STEP_IDLE, '')
            and str(goal.get('wanted')) == _WANT_RUNNING)
        projection['resumable'] = bool(
            run.get('end') is None
            and str(goal.get('wanted')) == _WANT_RUNNING
            and str(goal.get('start_kind')) == 'resume')
    else:
        projection['business_day'] = ''
        projection['has_active'] = bool(
            goal.get('wanted') == _WANT_RUNNING and step == _STEP_WAIT_TASKS)

    if goal.get('wanted') == 'stopped' and isinstance(run, dict) \
            and run.get('end') is None:
        projection['resumable'] = True
        projection['resume_blocked_reason'] = ''
    return projection
