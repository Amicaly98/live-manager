from types import SimpleNamespace

from app.api import settings as settings_module
from app import dependencies as dependencies_module
from app.core.email_sender import (
    EVENT_ACTION_REQUIRED,
    EVENT_LOCAL_FAIL,
    EVENT_RECOVERED,
    EmailSender,
)
from app.api.settings import AppSettings


def _sender_for_template(monkeypatch):
    """Build a sender with captured event submissions and no worker/network."""
    sender = EmailSender.__new__(EmailSender)
    captured = []

    def capture(event_id, subject, body, **kwargs):
        captured.append({
            'event_id': event_id,
            'subject': subject,
            'body': body,
            **kwargs,
        })
        return 'queued'

    sender.notify_event = capture
    sender._load_config = lambda: {
        'notify_complete': True,
        'notify_start': True,
        'notify_error': True,
    }
    sender._instance_label = lambda: 'desktop-test'
    return sender, captured


def test_desktop_face_mail_omits_unreachable_loopback_confirmation(monkeypatch):
    monkeypatch.delenv('SERVER_PUBLIC_HOST', raising=False)
    monkeypatch.delenv('API_PORT', raising=False)
    monkeypatch.delenv('API_SSL', raising=False)
    monkeypatch.setattr(
        settings_module, 'load_settings',
        lambda: SimpleNamespace(server_host=''))
    sender, captured = _sender_for_template(monkeypatch)
    monkeypatch.setattr(
        settings_module, 'load_settings',
        lambda: SimpleNamespace(server_host='localhost:8000'))
    assert sender._get_server_host() == ''
    assert sender._confirm_url('synthetic-token') == ''
    monkeypatch.setattr(
        settings_module, 'load_settings',
        lambda: SimpleNamespace(server_host=''))
    sender._face_verify_tokens = {}
    import threading
    sender._token_lock = threading.Lock()

    result = sender.send_face_verify(
        'https://www.bilibili.com/synthetic-verify',
        run_id='desktop-face-run', room_id=123, epoch=4,
    )

    assert result == 'queued'
    body = captured[0]['body']
    assert '127.0.0.1' not in body
    assert 'localhost' not in body
    assert '无法生成可点击的确认链接' in body
    assert '在验证弹窗中点击' in body


def test_desktop_daily_summary_is_short_and_lists_top_ten_priorities(monkeypatch):
    sender, captured = _sender_for_template(monkeypatch)
    rows = [
        {
            'zone_name': f'任务{i}', 'priority': i * 10,
            'days_done': i - 1, 'actual_days': 12,
            'remaining_exec_days': 13 - i,
        }
        for i in range(1, 11)
    ]

    sender.send_daily_summary(
        {
            'today_done': 2, 'today_pending': 11, 'pending_total': 11,
            'remaining_time': 132, 'urgency': 0.375,
        },
        rows,
        snapshot_meta={
            'done_flags': {'confident': True},
        },
        brief=True,
    )

    body = captured[0]['body']
    assert '接下来优先' in body
    assert '1. 任务1：优先度 10，已完成 0/12 天，还需 12 天' in body
    assert '10. 任务10：优先度 100，已完成 9/12 天，还需 3 天' in body
    assert '<table' not in body


def test_mail_templates_do_not_expose_instance_port_or_snapshot_metadata(monkeypatch):
    monkeypatch.delenv('INSTANCE_LABEL', raising=False)
    monkeypatch.setenv('API_PORT', '8000')
    sender, captured = _sender_for_template(monkeypatch)
    sender._instance_label = EmailSender._instance_label.__get__(sender)

    sender.send_daily_summary(
        {'today_done': 0, 'pending_total': 0,
         'remaining_time': 0, 'urgency': 0},
        [],
        snapshot_meta={'revision': 17, 'as_of': 'internal-capture-time'},
        brief=True,
    )

    subject = captured[0]['subject']
    body = captured[0]['body']
    assert subject.startswith('每日简报 · 桌面直播控制 · ')
    for private_value in ('instance-8000', '实例：', '版本', '采集',
                          'internal-capture-time'):
        assert private_value not in subject
        assert private_value not in body


def test_daily_summary_uses_cached_bilibili_username_without_network(monkeypatch):
    def unexpected_user_info_request():
        raise AssertionError('email rendering must not query Bilibili')

    controller = SimpleNamespace(
        cached_user_info={'uname': '本地缓存昵称'},
        api=SimpleNamespace(get_user_info=unexpected_user_info_request),
    )
    monkeypatch.setattr(
        dependencies_module, 'get_live_controller', lambda: controller)
    sender, captured = _sender_for_template(monkeypatch)
    sender._instance_label = EmailSender._instance_label.__get__(sender)

    sender.send_daily_summary(
        {'today_done': 0, 'pending_total': 0,
         'remaining_time': 0, 'urgency': 0},
        [], brief=True,
    )

    subject = captured[0]['subject']
    assert subject.startswith('每日简报 · 本地缓存昵称 · ')
    assert 'UID' not in subject and 'DedeUserID' not in subject


def test_daily_summary_falls_back_when_cached_bilibili_username_is_missing(
        monkeypatch):
    def unexpected_user_info_request():
        raise AssertionError('email rendering must not query Bilibili')

    controller = SimpleNamespace(
        cached_user_info={},
        api=SimpleNamespace(get_user_info=unexpected_user_info_request),
    )
    monkeypatch.setattr(
        dependencies_module, 'get_live_controller', lambda: controller)
    sender, captured = _sender_for_template(monkeypatch)
    sender._instance_label = EmailSender._instance_label.__get__(sender)

    sender.send_daily_summary(
        {'today_done': 0, 'pending_total': 0,
         'remaining_time': 0, 'urgency': 0},
        [], brief=True,
    )

    assert captured[0]['subject'].startswith('每日简报 · 桌面直播控制 · ')


def test_action_templates_keep_raw_status_and_run_identity_in_panel(monkeypatch):
    sender, captured = _sender_for_template(monkeypatch)

    sender.notify_action_required(
        'reconnect code=60024', 'platform status=60024',
        still_streaming=True, action='run_id=private-run')
    sender.notify_recovering(
        '明日方舟', attempt=17, reason='platform code=60024',
        run_id='private-run')
    sender.notify_local_stream_fail(
        '明日方舟', reason='FFmpeg code=1234', run_id='private-run')
    sender.notify_stopped(
        '明日方舟', stage='pending_recycle',
        reason='internal status=60024')

    rendered = '\n'.join(
        item['subject'] + '\n' + item['body'] for item in captured)
    for internal_value in (
            '60024', '1234', 'private-run', 'run_id=', 'API_PORT='):
        assert internal_value not in rendered


def test_rotation_notification_combines_completion_and_next_task(monkeypatch):
    sender, captured = _sender_for_template(monkeypatch)

    sender.notify_task_rotation(
        completion={
            'zone': '明日方舟', 'execution_date': '2026-09-25',
            'effective_seconds': 7560, 'days_done': 4,
        },
        next_task={'zone': '王者万象棋', 'duration_seconds': 7800},
        next_result='started', identity='rotation-1',
    )

    assert len(captured) == 1
    notice = captured[0]
    assert '明日方舟' in notice['subject'] and '王者万象棋' in notice['subject']
    assert '已完成任务：明日方舟' in notice['body']
    assert '下一个任务：王者万象棋' in notice['body']
    assert '结果：直播间已开启' in notice['body']
    assert notice['dedup_key'] == 'task-rotation:rotation-1:initial:started'


def test_platform_outage_terminal_notices_are_episode_scoped(monkeypatch):
    sender, captured = _sender_for_template(monkeypatch)

    sender.notify_platform_outage_result(
        '明日方舟', 'recovered', 'run-1', 7)
    sender.notify_platform_outage_result(
        '明日方舟', 'failed', 'run-1', 8)

    assert [item['event_id'] for item in captured] == [
        EVENT_RECOVERED, EVENT_ACTION_REQUIRED]
    assert captured[0]['subject'] == '直播连接已恢复 · 明日方舟'
    assert captured[1]['subject'] == '直播连接未能恢复 · 明日方舟'
    assert captured[0]['dedup_key'] != captured[1]['dedup_key']
    assert all('localhost' not in item['body'] for item in captured)


def test_short_local_ffmpeg_notice_is_off_by_default_and_optional():
    assert AppSettings().email_notify_ffmpeg_retry is False
    import queue
    import threading
    from app.core.email_sender import EVENT_SWITCH

    assert EVENT_SWITCH[EVENT_LOCAL_FAIL] == 'notify_ffmpeg_retry'
    sender = EmailSender.__new__(EmailSender)
    sender._last_send = {}
    sender._rate_lock = threading.Lock()
    sender._queue = queue.Queue()
    sender._counter_lock = threading.Lock()
    sender._counters = {}
    config = {
        'master_enabled': True, 'channel': 'email',
        'email_enabled': True, 'recipients': 'offline@example.invalid',
        'notify_ffmpeg_retry': False,
    }
    sender._load_config = lambda: dict(config)
    sender._use_email = lambda current: bool(
        current.get('email_enabled') and current.get('recipients'))
    sender._use_serverchan = lambda current: False

    assert sender.notify_local_stream_fail(
        '明日方舟', '短断流', run_id='short-drop') == 'disabled'
    assert sender._queue.qsize() == 0

    config['notify_ffmpeg_retry'] = True
    assert sender.notify_local_stream_fail(
        '明日方舟', '短断流', run_id='short-drop') == 'queued'
    assert sender._queue.qsize() == 1
