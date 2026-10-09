"""
dependencies.py - 依赖注入（全局单例持有者）

解决 app/main.py 和 app/api/*.py 之间的循环导入问题。
所有 API 模块通过此文件获取 task_manager / control_service / email_sender 实例。

阶段 4B 起，默认入口发布的是**唯一 owner** 的访问口：

- ``get_task_manager()``   —— 应用级 TaskManager（任务 API 与账号 TaskDataPort 共用）；
- ``get_control_service()``—— 唯一控制面（唯一运行意图权威）；
- ``get_application_owner()`` —— 应用资源所有者（健康/关闭门）；
- ``get_email_sender()``   —— 通知出口。

``get_live_controller()`` 保留函数名以兼容旧调用方，但**默认入口不再构造旧控制器**，
因此它在生产路径上返回 ``None``；API 不得依赖它存在。
"""

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ==================== 全局单例容器 ====================
_task_manager = None
_live_controller = None
_email_sender = None
_application_owner = None
_control_service = None
_lock = threading.RLock()


def init_globals(task_manager, live_controller, email_sender=None,
                 application_owner=None, control_service=None):
    """初始化全局单例（由 lifespan / 隔离夹具调用）。"""
    global _task_manager, _live_controller, _email_sender
    global _application_owner, _control_service
    with _lock:
        _task_manager = task_manager
        _live_controller = live_controller
        _email_sender = email_sender
        _application_owner = application_owner
        _control_service = control_service
    logger.info("全局依赖已初始化（旧控制器=%s）",
                'present' if live_controller is not None else 'none')


def publish_owner(owner) -> None:
    """发布**唯一 owner** 的访问口（lifespan 用；不经过旧三元组）。"""
    init_globals(owner.task_manager, None, owner.email_sender,
                 application_owner=owner, control_service=owner.control)


def clear_owner() -> None:
    """只清访问口，**不动**已保留的关闭报告（由调用方自行保存）。"""
    global _task_manager, _live_controller, _email_sender
    global _application_owner, _control_service
    with _lock:
        _task_manager = None
        _live_controller = None
        _email_sender = None
        _application_owner = None
        _control_service = None


def shutdown_globals():
    """清理全局单例（兼容旧调用方）。

    默认入口不再走这里——lifespan 调 :meth:`ApplicationOwner.close` 这**同一道**
    幂等关闭门；本函数只保留给旧夹具与旧测试。
    """
    global _task_manager, _live_controller, _email_sender
    logger.info("正在关闭全局依赖...")
    with _lock:
        controller, manager, sender = _live_controller, _task_manager, _email_sender
    if controller:
        try:
            controller.shutdown()
        except Exception as e:
            logger.error(f"直播控制器关闭异常: {e}")
    if manager:
        try:
            manager.shutdown()
        except Exception as e:
            logger.error(f"任务管理器关闭异常: {e}")
    # 控制器退出可能产生最后一条恢复/中断通知；其写入机会应先于
    # 邮件 worker 停止受理。通知器自己有总等待预算，不能拖过 systemd 窗口。
    if sender:
        try:
            sender.shutdown()
        except Exception as e:
            logger.error(f"邮件发送器关闭异常: {e}")
    clear_owner()
    logger.info("全局依赖已清理")


def get_task_manager():
    """获取**当前账号**的任务管理器实例。

    账号级数据路径（M0A）启用时，任务库属于当前已安装账号的绑定：这里每次都向
    owner 现取，因此换账号就换库、未登录就没有匿名任务库。兼容模式（或没有
    owner 的隔离夹具）仍返回 ``init_globals`` 发布的那一份。
    """
    owner = _application_owner
    if owner is not None:
        return getattr(owner, 'task_manager', None)
    return _task_manager


def get_account_binding():
    """当前已安装账号的**数据绑定**（兼容模式 / 未登录为 None）。"""
    owner = _application_owner
    if owner is None:
        return None
    return getattr(owner, 'account_binding', None)


def account_mode_enabled() -> bool:
    """当前 owner 是否启用**账号级数据路径**（同机共享根 + 按 UID 分库）。

    没有 owner 的隔离夹具、以及兼容模式（实例目录里的单份任务库）都返回 False：
    那两种现场里**没有第二个账号**，任务是实例归属的，"这次请求属于哪个 UID"这个
    问题不存在。账号模式才有"同一份 HTTP 入口先后服务不同 UID"的可能。
    """
    owner = _application_owner
    return bool(owner is not None and getattr(owner, 'account_mode', False))


def capture_account_work(expected_uid=None):
    """在请求**领取那一刻**冻结当前账号绑定，供排队执行点复核。

    见 ``session_runtime.accounts.FrozenAccountWork``：执行点用
    ``enter_valid_work()`` 核验并持有资源；失效即 :class:`AccountContextExpired`，
    调用方明确拒绝，绝不补最新账号后自动重放。没有 owner 的隔离夹具下冻结的是
    ``init_globals`` 发布的那一份任务库（等价于兼容模式）。

    ``expected_uid`` 是**请求自带的账号归属**（前端在用户点下操作、第一个确认 UI
    之前冻结，见 ``frontend/src/stores/tasks.ts``）。账号模式下它与当前绑定不一致
    时明确抛 :class:`AccountContextExpired`：旧页面/旧请求的意图**不接受**服务端
    用"最新账号"补一个上下文后执行。兼容模式与隔离夹具没有账号代次可比，原样忽略。
    """
    from app.core.session_runtime.accounts import (
        AccountContextExpired, FrozenAccountWork)

    owner = _application_owner
    if owner is None:
        # 没有账号代次可言（隔离夹具 / 兼容旧调用）：这里冻结不下任何账号资源，
        # 取库仍由调用方沿用它们一贯的入口（见 ``api.tasks._task_manager_or_409``）。
        return FrozenAccountWork(None, lambda: None)
    binding = getattr(owner, 'account_binding', None)
    if expected_uid is not None and account_mode_enabled():
        current = None if binding is None else int(binding.uid)
        if current != int(expected_uid):
            raise AccountContextExpired(
                '本次请求所属的账号（UID %s）与当前登录账号（%s）不一致：'
                '操作未执行，请刷新页面后按当前账号重新操作'
                % (int(expected_uid), '未登录' if current is None else f'UID {current}'))
    return FrozenAccountWork(binding,
                             lambda: getattr(owner, 'account_binding', None))


def get_live_controller():
    """旧控制器访问口：默认入口不再构造它，生产路径恒为 None。"""
    return _live_controller


def get_email_sender():
    """获取邮件发送器实例"""
    return _email_sender


def get_control_service():
    """获取唯一控制面（唯一运行意图权威）。"""
    return _control_service


def get_application_owner():
    """获取应用资源所有者（健康/关闭门）。"""
    return _application_owner


def get_account_display_name() -> str:
    """当前**已核验**账号的显示名（供通知署名；未登录/未核验时为空串）。

    只读内存中的已核验事实，不访问平台、不读凭据文件。
    """
    owner = _application_owner
    if owner is not None:
        name = str(getattr(owner, 'account_display_name', '') or '')
        if name:
            return name
        instance = getattr(owner, 'instance', None)
        account = getattr(instance, 'account', None)
        if account is not None:
            return str(getattr(account, 'display_name', '') or '')
    return ''
