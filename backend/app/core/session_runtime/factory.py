"""阶段 3C 的窄隔离组装入口。

调用者必须先在当前进程绑定 ``config.init(data_dir)``，并且只为这个 data_dir
创建一个 RuntimeInstance。该模块不会构造旧 LiveController、EmailSender 或 FastAPI app。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit


class InstanceConfigurationError(ValueError):
    """隔离路径、loopback endpoint 或测试凭据不满足边界。"""


class InstanceConstructionError(RuntimeError):
    """组装失败；cleanup 字段保留已经取得资源的实际清理结果。"""

    def __init__(self, cause: BaseException, cleanup: Dict[str, Any]):
        super().__init__(f'隔离实例组装失败：{type(cause).__name__}: {cause}')
        self.cause = cause
        self.cleanup = dict(cleanup)


class _RecordingNotifications:
    """隔离通知端口：仅记录，不发送 SMTP、Server酱或其他外部消息。"""

    def __init__(self) -> None:
        self.summaries = []
        self.messages = []

    def _instance_label(self) -> str:
        return 'phase3c-isolated'

    def send_daily_summary(self, stats, top5, summary_date=None,
                           snapshot_meta=None):
        self.summaries.append({
            'stats': dict(stats or {}), 'top5': list(top5 or []),
            'summary_date': summary_date, 'snapshot_meta': dict(snapshot_meta or {}),
        })
        return 'recorded'

    def send(self, *args, **kwargs):
        self.messages.append({'args': args, 'kwargs': kwargs})
        return 'recorded'


@dataclass(frozen=True)
class InstanceCloseReport:
    """Runtime 原始收尾结果与实例 Job 释放事实分开保存。

    ``task_manager_owned`` 是**责任归属**而不是结果：隔离实例（默认）自己拥有并
    关闭 TM；生产账号实例**借用**应用级共享 TM（见 ``PHASE4B-DESIGN.md`` §1），
    其 close 明确不动共享 TM，同时如实报告"这项资源不属于本实例"。
    判据 ``clean()`` 不变：借用方在 close 时按所有者约定**确实没有**待释放的
    关闭动作，因此不算"没关干净的共享 TM"。

    ``account_binding_released``：本实例是否持有一份**账号数据绑定**（M0A）。
    没有绑定时为 True（不适用）；有绑定却没收齐运行时/媒体/任务库句柄时为
    False —— 那时绑定与账号占用**继续保留**，不得靠 ``close`` 的返回伪装已释放。
    """

    runtime_report: Any = None
    job_released: bool = True
    job_error: str = ''
    task_manager_shutdown: bool = False
    task_manager_error: str = ''
    task_manager_owned: bool = True
    account_binding_released: bool = True
    account_binding_error: str = ''

    def to_dict(self) -> dict:
        return {
            'runtime': (self.runtime_report.to_dict()
                        if self.runtime_report is not None else None),
            'job_released': self.job_released,
            'job_error': self.job_error,
            'task_manager_shutdown': self.task_manager_shutdown,
            'task_manager_error': self.task_manager_error,
            'task_manager_owned': self.task_manager_owned,
            'account_binding_released': self.account_binding_released,
            'account_binding_error': self.account_binding_error,
        }

    def clean(self) -> bool:
        if self.runtime_report is None:
            return False
        clean = getattr(self.runtime_report, 'clean', None)
        return bool(clean and clean() and self.job_released
                    and not self.job_error and self.task_manager_shutdown
                    and not self.task_manager_error
                    and self.account_binding_released
                    and not self.account_binding_error)


class RuntimeInstance:
    """一个隔离目录中既有对象的所有者与统一关闭门。"""

    def __init__(self, *, data_dir: Path, video_base: Path, account: Any,
                 runtime: Any, media_port: Any, media_instance: Any,
                 task_manager: Any, task_port: Any, core: Any, store: Any,
                 platform_port: Any, notifications: Any, dependencies: Any,
                 previous_dependencies: tuple, clock: Any,
                 close_budget_seconds: float,
                 owns_task_manager: bool = True,
                 account_binding: Any = None):
        self.data_dir = data_dir
        self.video_base = video_base
        self.account = account
        self.runtime = runtime
        self.media_port = media_port
        self.media_instance = media_instance
        self.task_manager = task_manager
        self.task_port = task_port
        self.core = core
        self.store = store
        self.platform_port = platform_port
        self.clock = clock
        self.notifications = notifications
        self._dependencies = dependencies
        self._previous_dependencies = previous_dependencies
        self._close_budget_seconds = float(close_budget_seconds)
        #: 生产账号实例借用应用级共享 TM：close 不关它（见 InstanceCloseReport）。
        self._owns_task_manager = bool(owns_task_manager)
        #: M0A 账号数据绑定（并入实例的任务库/SessionStore 与账号占用）。
        #: 绑定附着在实例上：保留候选实例就等于保留它的账号占用。
        self.account_binding = account_binding
        self._close_lock = threading.Lock()
        self._close_report: Optional[InstanceCloseReport] = None
        self._started = False
        self._accepting = True
        self.inventory = None
        self.bootstrap_result = None
        self.inventory_result = None

    def start(self, *, timeout: float = 5.0, pre_bootstrap=None) -> dict:
        """先提交库存，后 Bootstrap；所有状态转换都由 Runtime actor 完成。

        ``pre_bootstrap`` 是应用层在 **Bootstrap 派发任何 Start 之前**的唯一一步
        （生产用它落实 ``.no_auto_resume``）：它拿到的实例已经起了 actor、提交过
        库存，但还没有 Bootstrap。它抛错时本次启动整体失败，调用方必须释放实例。
        """
        if self._started:
            return {'inventory': self.inventory, 'inventory_result': self.inventory_result,
                    'bootstrap_result': self.bootstrap_result}
        if not self._accepting:
            raise RuntimeError('隔离实例已经关闭')
        try:
            self.media_port.attach_sink(self.runtime.post_result)
            self.inventory = self.media_port.prepare_verified_inventory()
            self.runtime.start()
            if not self.runtime.started:
                raise RuntimeError(self.runtime.unsupported_reason or
                                   'Runtime 未能启动')
            # actor 已经跑起来了：实例此刻就是"可接收输入"的。Bootstrap 只是**第一条**
            # 待提交的输入，因此 `pre_bootstrap`（生产用它落实 .no_auto_resume）可以
            # 在它之前提交并等待自己的那条输入，而不必绕开实例的输入入口。
            self._started = True
            if pre_bootstrap is not None:
                pre_bootstrap(self)
            from ..session.events import Bootstrap, PusherInventory, Rejected
            self.inventory_result = self.runtime.submit(
                PusherInventory(owned=self.inventory.owned)).result(timeout)
            if isinstance(self.inventory_result, Rejected):
                raise RuntimeError(f'库存输入被拒绝：{self.inventory_result.reason}')
            self.bootstrap_result = self.runtime.submit(
                Bootstrap(reason='startup')).result(timeout)
            if isinstance(self.bootstrap_result, Rejected):
                raise RuntimeError(f'Bootstrap 被拒绝：{self.bootstrap_result.reason}')
            return {'inventory': self.inventory,
                    'inventory_result': self.inventory_result,
                    'bootstrap_result': self.bootstrap_result}
        except BaseException:
            raise

    def submit(self, event: Any, *, timeout: Optional[float] = None) -> Any:
        if not self._accepting:
            raise RuntimeError('隔离实例不再接收输入')
        if not self._started:
            raise RuntimeError('隔离实例尚未启动')
        return self.runtime.submit(event).result(timeout)

    def tasks_changed(self, business_day: str = '', reason: str = 'changed',
                      *, timeout: Optional[float] = None) -> Any:
        if not self._accepting:
            raise RuntimeError('隔离实例不再接收输入')
        if not self._started:
            raise RuntimeError('隔离实例尚未启动')
        return self.runtime.tasks_changed(business_day, reason).result(timeout)

    def snapshot_copy(self) -> Optional[dict]:
        return self.runtime.snapshot_copy()

    def wait_for_snapshot(self, predicate, timeout: Optional[float] = None
                          ) -> Optional[dict]:
        return self.runtime.wait_for_snapshot(predicate, timeout)

    @property
    def close_report(self) -> Optional[InstanceCloseReport]:
        return self._close_report

    def close(self, budget: Optional[float] = None) -> InstanceCloseReport:
        """按 Runtime → MediaInstance Job →（账号绑定 / TaskManager）顺序关闭。

        绑定在**运行时与媒体资源确实收齐之后**才释放：``clean`` 的“收齐”口径
        不变，只是账号模式下任务库与原生占用由绑定持有，因此它们也必须先确认
        收齐。收不齐时保留绑定与账号占用，不靠 ``close`` 的返回假装已释放。
        """
        with self._close_lock:
            if self._close_report is not None:
                return self._close_report
            self._accepting = False
            seconds = (self._close_budget_seconds if budget is None
                       else max(0.0, float(budget)))
            runtime_report = None
            job_released = self.media_instance is None
            job_error = ''
            manager_shutdown = False
            manager_error = ''
            binding_released = self.account_binding is None
            binding_error = ''
            try:
                runtime_report = self.runtime.close(seconds)
            except Exception as exc:  # noqa: BLE001
                job_error = f'runtime.close:{type(exc).__name__}: {exc}'
            try:
                if self.media_instance is not None:
                    self.media_instance.close()
                    job_released = self.media_instance.job is None
                    if not job_released:
                        job_error = job_error or 'MediaInstance Job 句柄仍未释放'
            except Exception as exc:  # noqa: BLE001
                job_error = job_error or f'{type(exc).__name__}: {exc}'
            runtime_clean = self._runtime_verified_clean(runtime_report)
            resources_clean = (runtime_clean and job_released and not job_error)
            if self.account_binding is not None:
                # 账号绑定持有任务库与原生占用：只有运行时/媒体确实收齐才释放。
                if not resources_clean:
                    binding_error = '运行时/媒体未收齐，账号绑定与账号占用继续保留'
                else:
                    try:
                        info = self.account_binding.close(seconds) or {}
                        manager_shutdown = bool(info.get('task_manager_shutdown'))
                        manager_error = str(info.get('task_manager_error') or '')
                        binding_released = bool(info.get('lock_released'))
                        if not binding_released:
                            binding_error = (str(info.get('lock_error') or '')
                                             or '账号占用未确认释放')
                    except Exception as exc:  # noqa: BLE001
                        binding_error = f'{type(exc).__name__}: {exc}'
            else:
                try:
                    if self._owns_task_manager:
                        self.task_manager.shutdown()
                    manager_shutdown = True
                except Exception as exc:  # noqa: BLE001
                    manager_error = f'{type(exc).__name__}: {exc}'
            try:
                previous = self._previous_dependencies
                # 生产账号实例借用应用级 owner 发布的全局依赖（``None``）：
                # 关闭时**不**恢复/清空，"谁发布谁负责"。
                if previous is not None:
                    self._dependencies.init_globals(*previous)
            except Exception as exc:  # noqa: BLE001
                manager_error = manager_error or (
                    f'dependencies.restore:{type(exc).__name__}: {exc}')
            self._close_report = InstanceCloseReport(
                runtime_report=runtime_report, job_released=job_released,
                job_error=job_error, task_manager_shutdown=manager_shutdown,
                task_manager_error=manager_error,
                task_manager_owned=self._owns_task_manager,
                account_binding_released=binding_released,
                account_binding_error=binding_error)
            return self._close_report

    @staticmethod
    def _runtime_verified_clean(runtime_report: Any) -> bool:
        """只回答"运行时的收尾是否通过核验"，与实例其它资源分开。"""
        if runtime_report is None:
            return False
        clean = getattr(runtime_report, 'clean', None)
        if not callable(clean):
            return False
        try:
            return bool(clean())
        except Exception:  # noqa: BLE001
            return False

    def __enter__(self) -> 'RuntimeInstance':
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _absolute(path: Any, name: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise InstanceConfigurationError(f'{name} 必须是绝对路径')
    return value.resolve()


def _validate_loopback(endpoint_base: str) -> str:
    if not endpoint_base:
        raise InstanceConfigurationError('必须显式提供 loopback endpoint')
    parsed = urlsplit(endpoint_base)
    if parsed.scheme != 'http' or not parsed.hostname or parsed.port is None:
        raise InstanceConfigurationError('endpoint 必须是带端口的 http loopback URL')
    host = parsed.hostname.strip('[]').lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if not loopback or parsed.username or parsed.password or parsed.path not in ('', '/'):
        raise InstanceConfigurationError('endpoint 不属于允许的 loopback 边界')
    return endpoint_base.rstrip('/')


def create_instance(*, data_dir: Any, video_base: Any, endpoint_base: str,
                    account: Any, credentials: Dict[str, str],
                    target_seconds: float = 3.0,
                    media_output: str = 'null:', realtime: bool = True,
                    op_budget_seconds: float = 2.0,
                    retry_delay_seconds: float = 0.1,
                    watch_interval_seconds: float = 0.1,
                    close_budget_seconds: float = 5.0,
                    instance_id: str = 'phase3c', seed: int = 20261002,
                    clock: Any = None
                    ) -> RuntimeInstance:
    """创建并启动一个隔离实例；不提供任何真实平台或通知默认值。"""
    data_dir = _absolute(data_dir, 'data_dir')
    video_base = _absolute(video_base, 'video_base')
    endpoint_base = _validate_loopback(endpoint_base)
    if not isinstance(credentials, dict) or not credentials.get('SESSDATA'):
        raise InstanceConfigurationError('必须显式传入隔离测试凭据')
    if not 0 < float(target_seconds) < 60:
        raise InstanceConfigurationError('测试目标时长必须在 (0, 60) 秒内')

    # 严格要求入口先绑定路径；这里不偷偷切换全局 config。
    from app.core import config as app_config
    data_dir.mkdir(parents=True, exist_ok=True)
    video_base.mkdir(parents=True, exist_ok=True)
    configured_data = app_config.get_data_path('_phase3c').parent.resolve()
    configured_video = (app_config.VIDEO_BASE_PATH.resolve()
                        if app_config.VIDEO_BASE_PATH is not None else None)
    if configured_data != data_dir:
        raise InstanceConfigurationError(
            f'config data_dir 与实例目录不同：{configured_data} != {data_dir}')
    if configured_video != video_base:
        raise InstanceConfigurationError(
            f'config video_base 与实例目录不同：{configured_video} != {video_base}')

    owned = {'task_manager': None, 'media_instance': None, 'runtime': None,
             'dependencies': None, 'previous': None, 'runtime_report': None,
             'job_released': False, 'task_manager_shutdown': False,
             'errors': []}
    try:
        # 所有这些导入都发生在调用者完成 config.init 之后。
        from app import dependencies
        from app.api.settings import AppSettings, load_settings, save_settings
        from app.core.task_manager import TaskManager
        from app.core.live_controller import BilibiliApi
        from app.core.session import AccountContext, Core, SystemClock, Tuning
        from app.core.session.store import SessionStore
        from app.core.session_runtime import (
            LocalCommandBuilder, MediaInstance, MediaPort, PlatformHttpPort,
            Runtime, TaskDataPort,
        )

        if not isinstance(account, AccountContext):
            raise InstanceConfigurationError('account 必须是 AccountContext')

        notification_sink = _RecordingNotifications()
        # 日切计算仍使用产品 settings 读取路径，但所有投递明确关闭并绑定记录器。
        save_settings(AppSettings(
            video_path=str(video_base),
            db_path=str(data_dir / 'live_tasks.db'),
            excel_path=str(data_dir / 'live_tasks.xlsx'),
            stream_mode='manual', notification_enabled=False,
            email_enabled=False, email_daily_summary=False,
            duration_multiplier_min=float(target_seconds) / 3600.0,
            duration_multiplier_max=float(target_seconds) / 3600.0,
        ))

        task_manager = TaskManager(
            db_path=str(data_dir / 'live_tasks.db'),
            excel_path=str(data_dir / 'live_tasks.xlsx'),
            reset_scheduler=False)
        owned['task_manager'] = task_manager
        previous_dependencies = (dependencies.get_task_manager(),
                                 dependencies.get_live_controller(),
                                 dependencies.get_email_sender())
        dependencies.init_globals(task_manager, None, notification_sink)
        owned['dependencies'] = dependencies
        owned['previous'] = previous_dependencies

        clock = clock or SystemClock()
        store = SessionStore(data_dir / 'live_state.json')
        core = Core(store, clock, account,
                    Tuning(op_budget_seconds=float(op_budget_seconds),
                           retry_delay_seconds=float(retry_delay_seconds),
                           watch_interval_seconds=float(watch_interval_seconds),
                           close_check_delay_seconds=0.0))
        task_port = TaskDataPort(
            task_manager, store, clock, account,
            area_lookup=lambda _zone: account.area_id)
        cookie_path = data_dir / 'phase3c_test_cookie_DO_NOT_CREATE.json'
        if cookie_path.exists():
            raise InstanceConfigurationError(
                f'测试 cookie 路径已存在，拒绝读取或覆盖：{cookie_path}')
        api = BilibiliApi(cookie_file=str(cookie_path))
        platform_port = PlatformHttpPort(api, clock, endpoint_base=endpoint_base)

        # Push source remains a Core Goal parameter. Keep a real inventory/cleanup
        # boundary for every goal, including OBS and restored FFmpeg-owned children.
        media_instance = MediaInstance(instance_id, data_dir)
        owned['media_instance'] = media_instance
        builder = LocalCommandBuilder(
            media_dir=media_instance.media_dir, video_base=video_base,
            default_folder=video_base / 'default', output=media_output,
            realtime=realtime, seed=seed, settings_reader=load_settings)
        media_port = MediaPort(
            media_instance, builder, attempt_budget_seconds=2.0,
            startup_observe_seconds=0.05, stall_window_seconds=1.0,
            gentle_slice_seconds=0.0, watch_interval_seconds=0.05,
            clock=clock, name=f'{instance_id}-media')

        runtime = Runtime(
            core, clock, platform_port=platform_port, task_port=task_port,
            media_port=media_port, credentials=lambda: dict(credentials),
            account=account, close_budget_seconds=close_budget_seconds,
            name=f'{instance_id}-runtime')
        owned['runtime'] = runtime
        handle = RuntimeInstance(
            data_dir=data_dir, video_base=video_base, account=account,
            runtime=runtime, media_port=media_port,
            media_instance=media_instance, task_manager=task_manager,
            task_port=task_port, core=core, store=store,
            platform_port=platform_port, notifications=notification_sink,
            dependencies=dependencies, previous_dependencies=previous_dependencies,
            clock=clock,
            close_budget_seconds=close_budget_seconds)
        handle.start()
        return handle
    except BaseException as exc:
        runtime_report = None
        job_released = owned['media_instance'] is None
        task_shutdown = owned['task_manager'] is None
        errors = []
        if owned['runtime'] is not None:
            try:
                runtime_report = owned['runtime'].close(close_budget_seconds)
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f'runtime:{type(cleanup_exc).__name__}: {cleanup_exc}')
        if owned['media_instance'] is not None:
            try:
                owned['media_instance'].close()
                job_released = owned['media_instance'].job is None
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f'job:{type(cleanup_exc).__name__}: {cleanup_exc}')
        if owned['task_manager'] is not None:
            try:
                owned['task_manager'].shutdown()
                task_shutdown = True
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f'task_manager:{type(cleanup_exc).__name__}: {cleanup_exc}')
        if owned['dependencies'] is not None and owned['previous'] is not None:
            try:
                owned['dependencies'].init_globals(*owned['previous'])
            except Exception as cleanup_exc:  # noqa: BLE001
                errors.append(f'dependencies:{type(cleanup_exc).__name__}: {cleanup_exc}')
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise InstanceConstructionError(exc, {
            'runtime': (runtime_report.to_dict()
                        if runtime_report is not None else None),
            'job_released': job_released,
            'task_manager_shutdown': task_shutdown,
            'errors': errors,
        }) from exc
