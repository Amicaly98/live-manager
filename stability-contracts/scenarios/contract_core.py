"""共同契约场景：与产品无关的时序与断言。

本模块**不 import 任何产品的 app 包**，也不重新实现被测逻辑。两端各自用
薄适配器（adapter_server / adapter_desktop）把这里的场景接到本仓真实入口。

规则：
- 同一场景、同一断言描述在两端都必须执行；产品之间的**有意差异**由适配器
  的 CAPS 显式声明（例如桌面重连入口不接收 epoch 参数、服务器停止是异步
  受理），不允许用 skip 隐藏失败。
- 适配器只允许做"构造真实控制器 / 调用真实入口 / 观察"，不得在适配器里
  重写被验证的状态机。
- 断言失败即失败；没有"环境跳过"这一档（环境不可用应如实报错）。
"""

import contextlib
import threading
import time

# 两端夹具都预置的合成分区
ZONE = '学习区'
ZONE_ALT = '游戏区'


class Spy:
    """替换某个方法为"记录调用 + 可选转发"的观察点。"""

    def __init__(self, impl=None):
        self.calls = []
        self._impl = impl

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._impl is not None:
            return self._impl(*args, **kwargs)
        return None

    @property
    def count(self):
        return len(self.calls)


class FakeLoop:
    """受控旧推流循环替身（**不创建真实线程**）。

    ``alive_sequence`` 依次返回，最后一个值保持；``on_join`` 在 ``join()``
    内执行，用于把"用户停止 / 新意图接管"精确插入到等待期间。
    """

    def __init__(self, alive_sequence=(True, False), on_join=None):
        self._alive = list(alive_sequence)
        self.on_join = on_join
        self.join_calls = 0

    def is_alive(self):
        if len(self._alive) > 1:
            return bool(self._alive.pop(0))
        return bool(self._alive[0])

    def join(self, timeout=None):
        self.join_calls += 1
        if self.on_join is not None:
            self.on_join(timeout)


class PusherLoopCounter:
    """统计"新建推流循环线程"的请求数。

    只按线程名 ``FFmpegLoop`` 计数，并且**真实创建仍然发生**（不改变被测
    行为、也不影响停止/开播线程）。用于验证"停止后不得另建循环"。
    """

    def __init__(self):
        self.count = 0

    def __enter__(self):
        outer = self
        real = threading.Thread

        class _CountingThread(real):
            def __init__(self, *args, **kwargs):
                if kwargs.get('name') == 'FFmpegLoop':
                    outer.count += 1
                super().__init__(*args, **kwargs)

        self._real = real
        threading.Thread = _CountingThread
        return self

    def __exit__(self, *exc):
        threading.Thread = self._real
        return False


@contextlib.contextmanager
def interleaved_worker(target, args, entered, release, timeout=15.0):
    """启动受控交错 worker，并保证失败路径也不会留下悬挂线程。

    - worker 进入等待点（``entered``）后把控制权交给场景主体；
    - 无论主体成功、断言失败还是抛异常，finally 都放行（``release``）并 join；
    - worker 内部异常（含断言）**回传到主线程**并让场景失败 —— 只看到"线程
      结束"不足以证明它正常跑完。
    """
    carrier = {}

    def _run():
        try:
            target(*args)
        except BaseException as exc:  # 断言失败也要在主线程复现
            carrier['error'] = exc

    worker = threading.Thread(target=_run, name='InterleavedWorker', daemon=True)
    worker.start()
    try:
        yield worker
    finally:
        release.set()
        worker.join(timeout)
    if carrier.get('error') is not None:
        raise AssertionError(
            f'worker 异常未回传（不得只凭线程结束判通过）：{carrier["error"]!r}'
        ) from carrier['error']
    assert not worker.is_alive(), 'worker 未在超时内结束'


class Scenario:
    def __init__(self, sid, group, title, fn):
        self.sid = sid
        self.group = group
        self.title = title
        self.fn = fn


SCENARIOS = []


def scenario(sid, group, title):
    def deco(fn):
        SCENARIOS.append(Scenario(sid, group, title, fn))
        return fn
    return deco


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ==================== CTRL-01：停止让此前操作失效 ====================

@scenario('CTRL-01a', 'CTRL-01',
          '首次开播：在缓存边界停止后不得提交在播状态、不得启动本地推流')
def ctrl01_initial_start_rechecks_after_cache(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, False)          # 首次开播：开始前未在播
        a.stop_is_acceptance_only(c)
        a.install_zone(c, ZONE)
        a.spy(c, '_extract_and_cache_rtmp', impl=lambda _resp: a.stop_user(c))
        pusher = a.spy(c, '_start_ffmpeg_stream')
        ok = a.initial_start_once(c, ZONE, a.epoch(c))
        assert ok is False, '停止已生效，开播必须未成立'
        assert a.is_streaming(c) is False, '停止后不得处于在播状态'
        assert pusher.count == 0, '停止后不得启动本地推流'
    finally:
        a.dispose(c)


@scenario('CTRL-01b', 'CTRL-01',
          '自动重连：平台返回后的缓存期间发生停止，不得触碰推流进程')
def ctrl01_reconnect_stop_during_cache(a):
    c = a.make('ffmpeg')
    try:
        a.stop_is_acceptance_only(c)
        a.install_zone(c, ZONE)
        kill = a.spy(c, '_kill_ffmpeg')
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.spy(c, '_extract_and_cache_rtmp', impl=lambda _resp: a.stop_user(c))
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        a.reconnect(c, a.epoch(c))
        assert pusher.count == 0, '停止后旧重连不得启动本地推流'
        assert kill.count == 0, (
            f'停止后旧重连不得触碰推流进程：_kill_ffmpeg 被调用 {kill.count} 次'
            '（旧实现先清理进程再检查停止）')
    finally:
        a.dispose(c)


@scenario('CTRL-01c', 'CTRL-01',
          '自动重连：停止后已建立新代（可复用事件被清除），旧响应不得提交缓存或推流')
def ctrl01_old_response_after_new_epoch(a):
    c = a.make('ffmpeg')
    try:
        a.stop_is_acceptance_only(c)
        a.install_zone(c, ZONE)
        epoch = a.epoch(c)

        def response(*_args):
            a.stop_user(c)
            a.model_taken_over_generation(c)
            return True, {'code': 0, 'data': {'source': 'OLD'}}

        a.set_start_live(c, response)
        cache = a.spy(c, '_extract_and_cache_rtmp')
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        a.reconnect(c, epoch)
        assert cache.count == 0, '旧代响应不得提交推流码缓存'
        assert pusher.count == 0, '旧代响应不得重启本地推流'
    finally:
        a.dispose(c)


@scenario('CTRL-01d', 'CTRL-01',
          '自动重连：停止发生在平台响应期间，必须被观测且不进入本地启动')
def ctrl01_reconnect_stop_inside_platform_response(a):
    c = a.make('ffmpeg')
    try:
        a.stop_is_acceptance_only(c)
        a.install_zone(c, ZONE)
        epoch = a.epoch(c)

        def response(*_args):
            a.stop_user(c)
            return True, {'code': 0}

        a.set_start_live(c, response)
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.reconnect(c, epoch)
        assert pusher.count == 0, '停止后不得进入本地推流启动'
    finally:
        a.dispose(c)


@scenario('CTRL-01e', 'CTRL-01',
          '受控交错：旧重连等待期间 停止→新开播→旧响应返回；新直播不得被清理或下播')
def ctrl01_interleaved_stop_then_new_intent(a):
    c = a.make('ffmpeg')
    try:
        a.install_zone(c, ZONE)
        entered = threading.Event()
        release = threading.Event()
        call_seq = {'n': 0}

        def start_live(room_id, area_id, csrf):
            call_seq['n'] += 1
            if call_seq['n'] == 1:
                # 旧重连的平台调用：挂住，等待场景放行
                entered.set()
                assert release.wait(15), '旧重连的平台响应未被放行'
            return True, {'code': 0, 'data': {
                'rtmp': {'addr': 'rtmp://127.0.0.1:1935/live-bvc', 'code': '?k=1'}}}

        a.set_start_live(c, start_live)
        cache = a.spy(c, '_extract_and_cache_rtmp')
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        epoch_at_entry = a.epoch(c)

        # worker 异常必须回传主线程；finally 无条件放行并回收线程
        with interleaved_worker(a.reconnect, (c, epoch_at_entry), entered, release):
            assert entered.wait(15), '旧重连未进入平台等待'

            # 1) 用户停止（真实停止入口，跑完清理）
            a.stop_user(c)
            a.wait_idle(c)
            intent_before_new = a.intent_id(c)

            # 2) 用户新开播（真实开播入口）——新意图接管
            assert a.start(c, ZONE) is True, '停止后用户发起的开播必须被受理'
            a.wait_idle(c)
            intent_after_new = a.intent_id(c)
            assert intent_after_new > intent_before_new, (
                '新开播必须登记新的开播意图序号（用于区分旧响应）')

            # 3) 记录旧响应到达前的基线
            cache_before = cache.count
            push_before = pusher.count
            stops_before = a.platform_call_count(c, 'stop_live')
            epoch_before = a.epoch(c)

        assert cache.count == cache_before, '旧响应不得提交推流码缓存'
        assert pusher.count == push_before, '旧响应不得新增本地推流启动'
        assert a.platform_call_count(c, 'stop_live') == stops_before, (
            '旧响应的补偿下播不得作用在新直播上（新 owner 未被下播）')
        assert a.epoch(c) == epoch_before, '旧响应不得推进/改动控制代际'
        assert a.intent_id(c) == intent_after_new, '旧响应不得清理新开播意图'
        assert a.current_zone(c) == ZONE, '旧响应不得清理新直播的任务/分区'
        assert a.is_streaming(c) is True, '新直播必须仍然在播'
    finally:
        try:
            c.stop_monitor.set()
            c._start_cancel.set()
        except Exception:
            pass
        a.dispose(c)


# ---------- CTRL-01f..i：推流启动内部的等待边界（真实启动方法，不整体替换） ----------
# 说明：CTRL-01a..e 从上位入口验证"停止后旧请求不得提交副作用"；下面四项直接
# 调用两端真实的 _start_ffmpeg_stream，只把"旧循环/杀进程/推流地址获取/新线程
# 创建"作为可控边界。停止一律走适配器的真实停止受理路径（不再在适配器里补
# 安全保护）。

@scenario('CTRL-01f', 'CTRL-01',
          '推流启动：等待旧循环期间 停止→新意图接管；旧等待返回后不得清理/覆盖新 owner')
def ctrl01f_stop_then_new_intent_during_old_loop_join(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stop_is_acceptance_only(c)
        kill = a.spy(c, '_kill_ffmpeg', impl=lambda *args, **kw: True)
        holder = {}

        def during_join(_timeout):
            # 用户停止（真实停止受理路径）
            a.stop_user(c)
            # 新意图接管：可复用事件被清除、在播标记复位（等价于用户随即重新开播）
            a.model_taken_over_generation(c)
            a.install_zone(c, ZONE)
            # 新会话已经登记的推流进程：旧请求绝不允许清理或替换它
            gen = a.new_pusher_generation(c)
            proc = a.make_unkillable_process(c)
            a.claim_pusher(c, gen, proc)
            holder['proc'] = proc

        old_loop = FakeLoop(alive_sequence=(True, False), on_join=during_join)
        a.set_old_loop(c, old_loop)
        with PusherLoopCounter() as create:
            result = a.start_pusher(c)

        assert result is False, '停止/新意图已接管：旧的推流启动请求必须作废'
        assert kill.count == 0, (
            f'失去所有权的旧请求不得清理推流进程：_kill_ffmpeg 被调用 {kill.count} 次')
        assert create.count == 0, '失去所有权的旧请求不得新建推流循环'
        assert a.pusher_loop(c) is old_loop, '旧请求不得覆盖/替换循环引用'
        assert a.stop_signal_set(c) is True, (
            '停止发生后旧请求不得清除停止信号（否则旧循环会被"复活"）')
        assert a.video_process(c) is holder['proc'], (
            '新 owner 已登记的推流进程不得被旧请求清理或替换')
    finally:
        a.dispose(c)


@scenario('CTRL-01g', 'CTRL-01',
          '推流启动：等待旧循环后仍未退出 → 保留引用与停止信号，不得另建循环')
def ctrl01g_unfinished_old_loop_keeps_reference(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        kill = a.spy(c, '_kill_ffmpeg', impl=lambda *args, **kw: True)
        old_loop = FakeLoop(alive_sequence=(True,))
        a.set_old_loop(c, old_loop)
        with PusherLoopCounter() as create:
            result = a.start_pusher(c)

        assert result is False, '旧循环仍在运行：不得启动新的推流循环'
        assert create.count == 0, '不得另建推流循环（否则同房间双推流）'
        assert kill.count == 0, '旧循环未退出时不得清理进程'
        assert a.pusher_loop(c) is old_loop, (
            '必须保留旧循环引用，交由正常恢复处理（不得先覆盖引用）')
        assert a.stop_signal_set(c) is True, '必须保留停止信号以便旧循环退出'
    finally:
        a.dispose(c)


@scenario('CTRL-01h', 'CTRL-01',
          '推流启动：网络获取推流地址期间 停止→新意图接管 → 返回后不得创建新循环')
def ctrl01h_stop_during_push_url_fetch(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stop_is_acceptance_only(c)
        a.spy(c, '_kill_ffmpeg', impl=lambda *args, **kw: True)
        a.stub_cached_push_url(c, None)  # 强制走网络获取分支

        def fetch(*_args):
            a.stop_user(c)                    # 网络等待期间受理停止
            # 新意图接管：可复用的停止事件被清除 —— 之后只有"原代际"这一判据
            # 能识别出本次启动请求已经过期（这正是本场景要验证的）。
            a.model_taken_over_generation(c)
            return True, {'push_url': 'rtmp://127.0.0.1/test'}

        a.set_push_url(c, fetch)
        with PusherLoopCounter() as create:
            result = a.start_pusher(c)

        assert result is False, '推流地址返回时本请求已过期：不得创建新的推流循环'
        assert create.count == 0, '过期请求不得创建新的推流循环'
    finally:
        a.dispose(c)


# ==================== CTRL-02：票据与停止重放 ====================

@scenario('CTRL-02a', 'CTRL-02',
          '旧停止重放只确认既有结果，绝不停掉后来明确开启的新直播')
def ctrl02_old_stop_replay_confirms_only(a):
    c = a.make('ffmpeg')
    try:
        old_ticket = a.issue_ticket(c)
        assert a.claim_stop(c, old_ticket) == 'execute', '首次停止应执行'
        a.stop_user(c)  # 停止执行 → 代际推进
        new_ticket = a.issue_ticket(c)
        assert a.begin_operation(c, new_ticket) is not None, '新意图的新票据必须有效'
        assert a.claim_stop(c, old_ticket) == 'confirm', '旧停止重放只能确认，不得再次执行'
        assert a.claim_stop(c, new_ticket) == 'execute', '新代际的停止应正常执行'
    finally:
        a.dispose(c)


@scenario('CTRL-02b', 'CTRL-02',
          '停止使旧票据失效；新意图签发新票据并可正常受理')
def ctrl02_tickets_follow_generation(a):
    c = a.make('ffmpeg')
    try:
        t1 = a.issue_ticket(c)
        assert a.begin_operation(c, t1) is not None, '当前代票据必须有效'
        a.stop_user(c)
        assert a.begin_operation(c, t1) is None, '停止后旧票据必须被拒绝（不再是当前代）'
        t2 = a.issue_ticket(c)
        assert a.begin_operation(c, t2) is not None, '停止后新签发的票据必须有效'
    finally:
        a.dispose(c)


@scenario('CTRL-02c', 'CTRL-02',
          '重复停止：清理已完成且无新会话时，重复请求只确认既有结果（不重复下播）')
def ctrl02c_repeated_stop_after_completion_only_confirms(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stop_user(c)
        a.wait_idle(c)
        stops_after_first = a.platform_call_count(c, 'stop_live')
        assert stops_after_first >= 1, '第一次停止必须真正执行一次平台下播'

        assert a.stop_user(c) is True, '重复的停止必须成功返回（幂等）'
        a.wait_idle(c)
        assert a.platform_call_count(c, 'stop_live') == stops_after_first, (
            '清理已完成且无新意图：重复停止不得再提交一次平台下播')
    finally:
        a.dispose(c)


@scenario('CTRL-02d', 'CTRL-02',
          '新直播之后的新停止仍然有效（幂等判据不得吃掉新会话的停止）')
def ctrl02d_stop_after_new_session_still_stops(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stop_user(c)
        a.wait_idle(c)
        stops_after_first = a.platform_call_count(c, 'stop_live')
        assert stops_after_first >= 1, '第一次停止必须真正执行一次平台下播'

        a.stop_user(c)                 # 重复停止：只确认
        a.wait_idle(c)

        assert a.start(c, ZONE) is True, '停止后用户开播必须被受理'
        a.wait_idle(c)
        assert a.is_streaming(c) is True, '新会话必须在播'

        assert a.stop_user(c) is True
        a.wait_idle(c)
        assert a.is_streaming(c) is False, '新会话的停止必须生效'
        assert a.platform_call_count(c, 'stop_live') == stops_after_first + 1, (
            '新会话的停止必须真正执行一次平台下播')
    finally:
        try:
            c.stop_monitor.set()
            c._start_cancel.set()
        except Exception:
            pass
        a.dispose(c)


@scenario('CTRL-02e', 'CTRL-02',
          '回收失败不得记为"清理完成"：保留 owner，后续显式停止仍能重试回收')
def ctrl02e_failed_reclaim_is_not_completion(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        proc = a.make_unkillable_process(c)
        a.claim_pusher(c, a.new_pusher_generation(c), proc)
        # 回收边界替换为"失败且保留 owner"的替身；停止入口/所有权登记/状态机
        # 全部走真实实现（本机不能真的造一个杀不掉的子进程）。
        failed = a.stub_failed_reclaim(c)

        assert a.stop_user(c) is True, '停止必须被受理'
        a.wait_idle(c)
        assert a.video_process(c) is proc, '回收失败必须保留 owned 进程引用'
        assert a.unrecycled(c) is True, '回收失败必须标记未回收'
        assert a.cleanup_done(c) is False, (
            '回收失败不得被记为"清理完成"：否则重复停止会被短路，连重试都没有')
        first_calls = failed.count
        assert first_calls > 0, '首次停止必须真实尝试回收'

        assert a.stop_user(c) is True, '重复的停止必须被受理'
        a.wait_idle(c)
        assert failed.count > first_calls, (
            '未确认回收时，重复停止必须允许受控重试'
            '（不得只把标记改成 False 却实际禁止再次回收）')
        assert a.cleanup_done(c) is False, '仍未回收 → 仍不得标记清理完成'
        assert a.video_process(c) is proc, '重试失败不得清除 owner 引用'
        assert a.unrecycled(c) is True, '重试失败必须保留未回收标记'
    finally:
        a.dispose(c)


@scenario('CTRL-02f', 'CTRL-02',
          '恢复闭环：重试回收成功后，后续重复停止只确认（不再重复回收/下播）')
def ctrl02f_successful_retry_then_confirm_only(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        proc = a.make_unkillable_process(c)
        a.claim_pusher(c, a.new_pusher_generation(c), proc)

        a.stub_failed_reclaim(c)
        assert a.stop_user(c) is True, '停止必须被受理'
        a.wait_idle(c)
        assert a.cleanup_done(c) is False, '前置条件：首次回收失败 → 不得标记完成'
        assert a.video_process(c) is proc, '前置条件：owner 引用仍保留'

        # 第二次停止：回收边界换成"本代进程确实退出"，走真实所有权清理路径
        ok_boundary = a.stub_successful_reclaim(c)
        assert a.stop_user(c) is True, '重复的停止必须被受理'
        a.wait_idle(c)
        assert ok_boundary.count > 0, '恢复阶段必须真的尝试回收'
        assert a.video_process(c) is None, '回收成功后必须清除引用'
        assert a.unrecycled(c) is False, '回收成功后必须清除未回收标记'
        assert a.cleanup_done(c) is True, '只有确认回收后才允许记为清理完成'

        calls_after_success = ok_boundary.count
        stops_after_success = a.platform_call_count(c, 'stop_live')
        assert a.stop_user(c) is True, '再次重复的停止必须成功返回（幂等）'
        a.wait_idle(c)
        assert ok_boundary.count == calls_after_success, (
            '清理已确认完成且无新会话：重复停止只确认，不得再次回收')
        assert a.platform_call_count(c, 'stop_live') == stops_after_success, (
            '清理已确认完成且无新会话：重复停止不得再提交一次平台下播')
    finally:
        a.dispose(c)


# ==================== PUSH-01：推流进程所有权 ====================

@scenario('PUSH-01a', 'PUSH-01',
          '未确认回收的推流进程：保留引用并禁止重复启动同房间推流')
def push01_unrecycled_blocks_new_pusher(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        proc = a.make_unkillable_process(c)
        gen = a.new_pusher_generation(c)
        a.claim_pusher(c, gen, proc)
        a.reclaim(c, timeout=1.0)
        assert a.video_process(c) is proc, '未确认回收必须保留原引用'
        assert a.unrecycled(c) is True, '未确认回收必须标记 _ffmpeg_unrecycled'
        assert a.start_pusher(c) is False, '未回收时禁止另起一路同房间推流'
        assert a.video_process(c) is proc, '被拒绝的启动不得改动引用'
    finally:
        a.dispose(c)


@scenario('PUSH-01b', 'PUSH-01',
          '旧代回收不得清除新代推流引用（代际所有权）')
def push01_old_generation_cannot_clear_new_reference(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        old_proc = a.make_unkillable_process(c)
        gen1 = a.new_pusher_generation(c)
        a.claim_pusher(c, gen1, old_proc)
        # 新代接管（模拟重连/恢复新建一路）
        a.install_zone(c, ZONE)
        gen2 = a.new_pusher_generation(c)
        new_proc = a.make_finished_process()
        a.claim_pusher(c, gen2, new_proc)
        # 旧代的回收迟到：不得清除新代引用
        a.release_pusher(c, gen1)
        assert a.video_process(c) is new_proc, '旧代不得清除新代引用'
    finally:
        a.dispose(c)


# ==================== MODE-01：task/manual × FFmpeg/OBS ====================

@scenario('MODE-01a', 'MODE-01',
          'OBS 模式（stream_mode=manual）：恢复/重连不得启动本地 FFmpeg')
def mode01_obs_does_not_start_local_pusher(a):
    c = a.make('manual')
    try:
        a.stop_is_acceptance_only(c)
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.reconnect(c, a.epoch(c))
        assert pusher.count == 0, 'OBS 外部推流不得被本地 FFmpeg 抢流'
    finally:
        a.dispose(c)


@scenario('MODE-01b', 'MODE-01',
          'FFmpeg 模式：恢复/重连必须恢复本地推流（手动分区同样适用）')
def mode01_ffmpeg_restores_local_pusher(a):
    c = a.make('ffmpeg')
    try:
        a.stop_is_acceptance_only(c)
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.stub_cached_push_url(c, 'rtmp://127.0.0.1/test')
        pusher = a.spy(c, '_start_ffmpeg_stream')
        a.reconnect(c, a.epoch(c))
        assert pusher.count == 1, 'FFmpeg 模式的自动恢复必须重启本机推流（恰好一次）'
    finally:
        a.dispose(c)


# ==================== INTENT-01：停止意图与可继续进度 ====================

@scenario('INTENT-01a', 'INTENT-01',
          '人为停止保留可继续进度并持久化停止意图')
def intent01_user_stop_preserves_resume(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.mark_progress(c, 600)
        a.stop_user(c)
        a.wait_idle(c)
        assert a.stop_intent_present(c) is True, '人为停止必须持久化停止意图'
        assert a.resumable_elapsed(c) >= 600, (
            f'停止不得丢失可继续进度：保留 {a.resumable_elapsed(c)} 秒')
    finally:
        a.dispose(c)


@scenario('INTENT-01b', 'INTENT-01',
          '成功的新开播清除停止意图（用户新意图优先）')
def intent01_successful_start_clears_stop_intent(a):
    c = a.make('ffmpeg')
    try:
        a.set_streaming(c, True)
        a.install_zone(c, ZONE)
        a.mark_progress(c, 600)
        a.stop_user(c)
        a.wait_idle(c)
        assert a.stop_intent_present(c) is True, '前置条件：停止意图已记录'
        assert a.start(c, ZONE) is True, '停止后用户开播必须被受理'
        a.wait_idle(c)
        assert a.is_streaming(c) is True, '开播应已生效'
        assert a.stop_intent_present(c) is False, '成功开播后必须清除停止意图'
    finally:
        try:
            c.stop_monitor.set()
            c._start_cancel.set()
        except Exception:
            pass
        a.dispose(c)


def run_all(adapter, on_result=None):
    """顺序执行全部场景；返回 [{sid, group, title, ok, error}]。"""
    results = []
    for sc in SCENARIOS:
        ok = True
        error = ''
        try:
            sc.fn(adapter)
        except Exception as exc:  # 断言失败与环境错误分开记录
            ok = False
            error = f'{type(exc).__name__}: {exc}'
        results.append({'sid': sc.sid, 'group': sc.group, 'title': sc.title,
                        'ok': ok, 'error': error})
        if on_result:
            on_result(results[-1])
    return results
