"""桌面通知守卫 + 一次真实模板/队列演练（桌面移植阶段 2 重写）。

旧版本（1.1.1）的这份文件断言的是**已退役的桌面通知实现**的文案与内部字段
（``notify_task_rotation(completion=..., next_task=...)`` 的旧签名、``brief=``
参数、``无法生成可点击的确认链接`` 等）。阶段 1 把 ``email_sender.py`` 换成共享
链之后，那些断言测的已不是仓库里的任何代码——它们在 HEAD ``f943b43`` 上就是
8 条红（夹具用 ``EmailSender.__new__`` 绕过了 ``__init__``，共享实现的新字段
``_host_cache`` 等根本不存在）。

本轮按**断言职责**把它们迁到新链上，而不是停收集后写一句"由某实现承担"：

- 人脸邮件不拼不可达的 loopback 确认链接 → 新链的 ``host_usable`` 判据 + 文案；
- 每日简报简短、不暴露实例端口/快照内部字段 → ``send_daily_summary``；
- 署名用**冻结**账号名、渲染期不联网问 B 站 → ``_summary_account_name``；
- 轮转一封合并"已完成 + 下一场结果" → ``notify_task_rotation``；
- 平台中断的终结通知按场次去重 → ``notify_platform_outage_result``；
- 本地 FFmpeg 短断流默认关闭、可选开启 → ``EVENT_LOCAL_FAIL`` 的独立开关；
- 受理与"正在恢复"都不发邮件 → ``suppressed``（受理 ≠ 平台事实）。

**真实演练**在最后两个用例：用**真的** ``EmailSender``（真工作线程、真的队列、
真的限频与模板渲染），只把**物理投递出口**（``_do_send_email`` /
``_send_serverchan``）换成记录器。不连 SMTP、不连 Server酱、不连 B 站。
"""

import datetime
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import settings as settings_module
from app import dependencies as dependencies_module
from app.core import email_sender as email_module
from app.core.email_sender import (
    EVENT_ACTION_REQUIRED, EVENT_LOCAL_FAIL, EVENT_RECOVERED, EVENT_STOPPED,
    EVENT_TASK_COMPLETE, EVENT_TASK_ROTATION, EmailSender,
)
from app.api.settings import AppSettings
from app.core.session_runtime.desktop import desktop_notifier_policy


BASE_CONFIG = {
    'master_enabled': True,
    'channel': 'email',
    'email_enabled': True,
    'smtp_host': 'smtp.invalid',
    'smtp_port': 587,
    'smtp_user': 'drill@example.invalid',
    'smtp_pass': 'synthetic-password-not-real',
    'recipients': 'drill@example.invalid',
    'serverchan_sendkey': '',
    'notify_start': True,
    'notify_stop': True,
    'notify_error': True,
    'notify_complete': True,
    'daily_summary': True,
    'notify_ffmpeg_retry': False,
    'face_verify_port': 19080,
}

#: 队列排空的有界等待：演练要等真实工作线程，但不能用无限等待掩盖卡死。
DRAIN_TIMEOUT_SECONDS = 5.0


class _Recorder:
    """物理投递出口的替身：只记录，不做任何网络动作。"""

    def __init__(self):
        self.sent = []

    def email(self, config, subject, body, html=False):
        self.sent.append({'channel': 'email', 'subject': subject,
                          'body': body, 'html': html})
        return True

    def serverchan(self, title, content):
        self.sent.append({'channel': 'serverchan', 'title': title,
                          'body': content})
        return True


def drain(real: EmailSender, expected: int, recorder: _Recorder,
          timeout: float = DRAIN_TIMEOUT_SECONDS) -> bool:
    """等真实队列被工作线程消费完（有界），返回是否达到预期投递条数。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(recorder.sent) >= expected and real.stats()['queue_size'] == 0:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sender(monkeypatch):
    """真实 EmailSender（真工作线程 + 真队列），只换物理出口。"""
    recorder = _Recorder()
    config = dict(BASE_CONFIG)
    real = EmailSender()
    monkeypatch.setattr(real, '_load_config', lambda: dict(config))
    monkeypatch.setattr(real, '_do_send_email', recorder.email)
    monkeypatch.setattr(real, '_send_serverchan', recorder.serverchan)
    monkeypatch.setattr(real, '_instance_label', lambda: 'desktop-test')
    # 桌面装配策略：不为构造确认链接去探测云元数据地址。
    desktop_notifier_policy(real)
    yield real, recorder, config
    real.shutdown()


# ==================== 桌面装配：不探测云元数据 ====================

def test_desktop_notifier_disables_cloud_metadata_probe(monkeypatch):
    """桌面装配之后不再为确认链接去连云元数据地址；服务端默认语义不变。"""
    monkeypatch.delenv('SERVER_PUBLIC_HOST', raising=False)
    monkeypatch.setattr(settings_module, 'load_settings',
                        lambda: SimpleNamespace(server_host=''))

    # 记录"真的会去连"的那一步（线程被换成记录器，因此判定是确定的）。
    calls = []

    class _FakeThread:
        def __init__(self, **kwargs):
            calls.append(kwargs.get('target'))

        def start(self):
            return None

    monkeypatch.setattr(email_module.threading, 'Thread', _FakeThread)

    sender = EmailSender.__new__(EmailSender)
    sender._host_cache = None
    sender._host_probe_started = False
    sender._host_resolution_error = ''
    sender._closing = threading.Event()

    desktop_notifier_policy(sender)
    assert sender._host_probe_started is True, '禁用标记 = 已判过，不再探测'
    # 没有可用地址时共享实现回落成 'localhost'；它**不是**可用远程地址——
    # `host_usable` 会因此判假，确认链接一律不拼（见下一个用例）。
    assert sender._get_server_host() == 'localhost'
    assert sender._confirm_url('tok') == ''
    assert calls == [], '桌面装配后不得再发起云元数据探测'

    # 服务端默认（不调用桌面策略）：仍走既有探测路径 —— 共享语义未被反向修改。
    plain = EmailSender.__new__(EmailSender)
    plain._host_cache = None
    plain._host_probe_started = False
    plain._host_resolution_error = ''
    plain._closing = threading.Event()
    plain._get_server_host()
    assert len(calls) == 1, '未装配桌面策略时应保持既有的后台探测行为'
    assert calls[0] == plain._probe_host


def test_configured_public_host_is_used_for_confirm_link(monkeypatch):
    """有用户配置的可用地址时按实际能力给出链接；localhost 不算可用地址。"""
    monkeypatch.delenv('SERVER_PUBLIC_HOST', raising=False)
    monkeypatch.delenv('API_SSL', raising=False)
    monkeypatch.setenv('API_PORT', '8000')

    sender = EmailSender.__new__(EmailSender)
    sender._host_cache = None
    sender._host_probe_started = True          # 装配后：不再探测
    sender._host_resolution_error = 'desktop_product'
    sender._closing = threading.Event()

    monkeypatch.setattr(settings_module, 'load_settings',
                        lambda: SimpleNamespace(server_host='127.0.0.1'))
    assert sender._get_server_host() == '', 'loopback 不能当公网地址'
    assert sender._confirm_url('tok') == ''

    monkeypatch.setattr(settings_module, 'load_settings',
                        lambda: SimpleNamespace(server_host='panel.example.org'))
    assert sender._get_server_host() == 'panel.example.org'
    assert sender._confirm_url('tok') == (
        'http://panel.example.org:8000/api/email/confirm-face-verify?token=tok')


# ==================== 人脸邮件：不宣传不可达链接 ====================

def test_desktop_face_mail_omits_unreachable_loopback_confirmation(monkeypatch,
                                                                   sender):
    monkeypatch.delenv('SERVER_PUBLIC_HOST', raising=False)
    monkeypatch.delenv('API_SSL', raising=False)
    real, recorder, _config = sender
    monkeypatch.setattr(settings_module, 'load_settings',
                        lambda: SimpleNamespace(server_host=''))

    deferred = real.send_face_verify(
        'https://www.bilibili.com/synthetic-verify',
        run_id='desktop-face-run', room_id=123, epoch=4,
        defer_notification=True)

    assert deferred['confirm_url'] == ''
    assert real.notify_event(
        EVENT_ACTION_REQUIRED, '[人脸] 合成',
        real._face_lines('', '合成分区', 'https://www.bilibili.com/v', ''),
        wait=True) == 'sent'
    body = recorder.sent[-1]['body']
    assert '127.0.0.1' not in body
    assert 'localhost' not in body
    assert '验证后请在面板点击“验证完成”。' in body


# ==================== 每日简报 ====================

def test_desktop_daily_summary_is_short_and_lists_priorities(sender):
    real, recorder, _config = sender
    rows = [
        {
            'zone_name': f'任务{i}', 'priority': i * 10,
            'days_done': i - 1, 'actual_days': 12,
            'remaining_exec_days': 13 - i,
        }
        for i in range(1, 11)
    ]
    day = datetime.date(2026, 10, 10)

    assert real.send_daily_summary(
        {'today_done': 2, 'today_pending': 11, 'pending_total': 11,
         'remaining_time': 132, 'urgency': 0.375},
        rows, summary_date=day,
        snapshot_meta={'done_flags': {'confident': True, 'window': day}}) == 'queued'
    assert drain(real, 1, recorder)

    body = recorder.sent[-1]['body']
    assert '接下来优先' in body
    assert '<li>任务1：优先度 10，已完成 0/12 天，还需 12 天</li>' in body
    assert '<li>任务10：优先度 100，已完成 9/12 天，还需 3 天</li>' in body
    assert '2026-10-10 直播简报' in body


def test_desktop_runner_supplies_port_free_instance_label():
    """桌面入口给通知署名一个不带端口的稳定回落值（共享实现的顺序未改）。"""
    source = (Path(__file__).resolve().parents[1] / 'run.py').read_text(
        encoding='utf-8')
    assert 'os.environ.setdefault("INSTANCE_LABEL", "desktop")' in source
    # 回落顺序本身是服务端能力（端口区分多实例），桌面只是**不用**它：
    # 共享实现里的顺序与措辞一个字都未改。
    email_source = (Path(__file__).resolve().parents[1]
                    / 'app' / 'core' / 'email_sender.py').read_text(
        encoding='utf-8')
    assert "return f'instance-{port}'" in email_source


def test_daily_summary_does_not_expose_instance_port_or_snapshot_metadata(
        monkeypatch, sender):
    monkeypatch.setenv('API_PORT', '8000')
    # 桌面入口实际设置的中性实例标识（见上一个用例）。
    monkeypatch.setenv('INSTANCE_LABEL', 'desktop')
    real, recorder, _config = sender
    # 用**真实**的 _instance_label，确认它不会被写进正文（只进去重键）。
    monkeypatch.setattr(real, '_instance_label',
                        EmailSender._instance_label.__get__(real))

    assert real.send_daily_summary(
        {'today_done': 0, 'pending_total': 0, 'remaining_time': 0,
         'urgency': 0},
        [], summary_date=datetime.date(2026, 10, 10),
        snapshot_meta={'revision': 17, 'as_of': 'internal-capture-time'}) == 'queued'
    assert drain(real, 1, recorder)

    subject = recorder.sent[-1]['subject']
    body = recorder.sent[-1]['body']
    assert subject.startswith('[简报] · ')
    for private_value in ('instance-', '8000', 'internal-capture-time',
                          '采集'):
        assert private_value not in subject
        assert private_value not in body
    # 完成数窗口与统计日不一致时如实说"待确认"，不把历史完成硬贴成当日结果。
    assert '完成数暂未核实' in body


def test_daily_summary_uses_frozen_account_name_without_network(monkeypatch,
                                                                sender):
    def unexpected_user_info_request():
        raise AssertionError('email rendering must not query Bilibili')

    monkeypatch.setattr(dependencies_module, 'get_live_controller',
                        lambda: SimpleNamespace(
                            api=SimpleNamespace(
                                get_user_info=unexpected_user_info_request)))
    monkeypatch.setattr(dependencies_module, 'get_account_display_name',
                        lambda: '本地缓存昵称')
    real, recorder, _config = sender
    monkeypatch.setattr(real, '_instance_label',
                        EmailSender._instance_label.__get__(real))

    assert real.send_daily_summary(
        {'today_done': 0, 'pending_total': 0, 'remaining_time': 0,
         'urgency': 0}, [], summary_date=datetime.date(2026, 10, 10)) == 'queued'
    assert drain(real, 1, recorder)

    subject = recorder.sent[-1]['subject']
    assert subject.startswith('[简报] · 本地缓存昵称 · ')
    assert 'UID' not in subject and 'DedeUserID' not in subject


def test_daily_summary_falls_back_when_account_name_is_missing(monkeypatch,
                                                               sender):
    monkeypatch.setattr(dependencies_module, 'get_account_display_name',
                        lambda: '')
    real, recorder, _config = sender

    assert real.send_daily_summary(
        {'today_done': 0, 'pending_total': 0, 'remaining_time': 0,
         'urgency': 0}, [], summary_date=datetime.date(2026, 10, 10)) == 'queued'
    assert drain(real, 1, recorder)

    # 没有账号名时退回简短实例标识，不再暴露端口或内部快照字段。
    assert recorder.sent[-1]['subject'].startswith('[简报] · desktop-test · ')


# ==================== 受理/恢复过程不发邮件 ====================

def test_acceptance_and_recovery_process_do_not_send_mail(sender):
    real, recorder, _config = sender
    # 受理只表示意图已落盘：没有任何"已开播"的事实可写，因此**不发**邮件。
    assert real.notify_start_accepted('明日方舟', '2小时0分钟') == 'suppressed'
    assert real.notify_recovering(
        '明日方舟', attempt=3, reason='platform code=60024') == 'suppressed'
    assert recorder.sent == []


# ==================== 动作/轮转/中断 ====================

def test_action_templates_keep_raw_status_and_run_identity_out_of_mail(sender):
    real, recorder, _config = sender

    # 需人工处理：只写"需要处理 + 去面板看"，技术细节留在面板与日志。
    assert real.notify_action_required(
        '自动恢复持续失败', 'platform code=60024', still_streaming=True,
        action='run_id=private-run', identity='drill-1') == 'queued'
    # 停止结果待确认是**独立**的已确认问题：按 [异常] 发，不混着说"已停止"。
    assert real.notify_stopped(
        '明日方舟', stage='pending_recycle',
        reason='internal status=60024') == 'queued'
    assert drain(real, 2, recorder)

    rendered = '\n'.join(item['subject'] + '\n' + item['body']
                         for item in recorder.sent)
    for internal_value in ('60024', 'private-run', 'run_id=', 'API_PORT='):
        assert internal_value not in rendered
    assert '停止请求已受理，但结果待确认' in rendered


def test_rotation_notification_combines_completion_and_next_task(sender):
    real, recorder, _config = sender

    assert real.notify_task_rotation(
        completion={'zone': '明日方舟', 'execution_date': '2026-10-10',
                    'effective_seconds': 7560, 'days_done': 4},
        next_task={'zone': '王者万象棋', 'duration_seconds': 7800},
        next_result='started', identity='rotation-1',
        account_name='冻结账号') == 'queued'
    assert drain(real, 1, recorder)

    item = recorder.sent[-1]
    assert item['subject'] == '[轮转] · 冻结账号 · 明日方舟 → 王者万象棋'
    assert '明日方舟已完成（执行日 2026-10-10）。' in item['body']
    assert '下一个任务：王者万象棋' in item['body']
    assert '结果：王者万象棋已开播。' in item['body']


def test_platform_outage_terminal_notices_are_episode_scoped(sender):
    real, recorder, _config = sender

    assert real.notify_platform_outage_result(
        '明日方舟', 'recovered', 'run-1', 7) == 'queued'
    assert drain(real, 1, recorder)
    first_dedup = real._last_send[(EVENT_RECOVERED, 'run-1:7:platform-outage')]

    # 同一场次同一故障：限频合并，不重复打扰。
    assert real.notify_platform_outage_result(
        '明日方舟', 'recovered', 'run-1', 7) == 'coalesced'
    assert real._last_send[(EVENT_RECOVERED, 'run-1:7:platform-outage')] == first_dedup

    # 另一段故障（episode 不同）= 另一个身份：不被上一个窗口吃掉。
    assert real.notify_platform_outage_result(
        '明日方舟', 'failed', 'run-1', 8) == 'queued'
    assert drain(real, 2, recorder)

    subjects = [item['subject'] for item in recorder.sent]
    assert subjects == ['[恢复] · desktop-test · 明日方舟',
                        '[异常] · desktop-test · 明日方舟']
    assert all('localhost' not in item['body'] for item in recorder.sent)


def test_short_local_ffmpeg_notice_is_off_by_default_and_optional(sender):
    real, recorder, config = sender
    assert AppSettings().email_notify_ffmpeg_retry is False

    # 默认关闭：短断流不入队（连队列都没进）。
    assert real.notify_local_stream_fail(
        '明日方舟', '短断流', run_id='short-drop') == 'disabled'
    assert recorder.sent == []

    # 显式开启后才入队——开启与否是用户设置，不是新逻辑。
    config['notify_ffmpeg_retry'] = True
    assert real.notify_local_stream_fail(
        '明日方舟', '短断流', run_id='short-drop') == 'queued'
    assert drain(real, 1, recorder)
    assert recorder.sent[0]['subject'] == '[异常] · desktop-test · 明日方舟'


# ==================== 一次真实模板 + 队列演练 ====================

def test_notification_drill_runs_real_queue_with_synthetic_delivery(sender):
    """真实演练：真工作线程 + 真队列 + 真模板，只换物理投递出口。

    核对三件事：
      1. 每条通知都真的经队列 → 工作线程 → 投递出口（不是"入队即算送达"）；
      2. 署名取**冻结**的账号名（晚投递不改写）；
      3. 正文里没有内部端口/快照/run_id 一类私有字段。
    """
    real, recorder, _config = sender

    assert real.notify_stream_running(
        '明日方舟', 'new', '0秒（0秒）', '2小时0分钟', run_id='drill-run-1',
        account_name='冻结账号') == 'queued'
    assert real.notify_stopped(
        '明日方舟', stage='stopped', reason='user', account_name='冻结账号',
    ) == 'queued'
    assert real.notify_task_complete(
        '明日方舟', '2026-10-10', 'today', 'settled',
        account_name='冻结账号') == 'queued'
    assert real.send_test() == 'sent'          # 测试通知等真实投递结果

    assert drain(real, 4, recorder)
    assert [item['channel'] for item in recorder.sent] == ['email'] * 4
    # `send_test()` 是渠道测试，没有账号上下文：署名回落到实例标识，这是预期的。
    named = [item for item in recorder.sent if item['subject'] != '[测试] · desktop-test']
    assert len(named) == 3
    for item in named:
        assert '· 冻结账号 ·' in item['subject']
    for item in recorder.sent:
        assert 'instance-' not in item['body']
        assert 'run_id=' not in item['body']
    assert '[开播] · 冻结账号 · 明日方舟' in [i['subject'] for i in recorder.sent]
    assert '[停止] · 冻结账号 · 明日方舟' in [i['subject'] for i in recorder.sent]

    stats = real.stats()
    assert stats['counters']['sent'] == 4
    assert stats['counters']['failed'] == 0
    assert stats['counters']['queued'] == 4
    assert stats['queue_size'] == 0
    assert stats['channels']['email'] == {'sent': 4, 'failed': 0}


def test_notification_drill_never_touches_real_transports(monkeypatch, sender):
    """演练期间不得建立任何真实外发连接（SMTP / Server酱 / 云元数据）。"""
    real, recorder, _config = sender

    def forbidden(*args, **kwargs):
        raise AssertionError('演练期间不得调用真实物理投递')

    monkeypatch.setattr(email_module, '_requests', SimpleNamespace(
        get=forbidden, post=forbidden))
    monkeypatch.setattr(email_module.smtplib, 'SMTP', forbidden)
    monkeypatch.setattr(email_module.smtplib, 'SMTP_SSL', forbidden)

    assert real.send_test() == 'sent'
    assert real.notify_action_required('演练', 'synthetic',
                                       identity='drill-2') == 'queued'
    assert drain(real, 2, recorder)
