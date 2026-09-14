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

        worker = threading.Thread(target=a.reconnect, args=(c, epoch_at_entry),
                                  name='OldReconnect', daemon=True)
        worker.start()
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

        release.set()
        worker.join(15)
        assert not worker.is_alive(), '旧重连线程未结束'

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
