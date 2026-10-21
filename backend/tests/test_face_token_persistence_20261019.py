"""人脸确认令牌：不限期 + 一次性 + 跨重启持久化（2026-10-19）。

覆盖：
1. 默认签发不带过期时间，时间前推仍可消费；显式 TTL 仍然过期；
2. 令牌写入实例数据目录的 face_verify_tokens.json（POSIX 0600），重启后可继续使用；
3. 一次性消费先落盘：消费后重启不可重放；
4. 损坏文件自愈；签发落盘失败时回退面板确认，消费落盘失败可重试；
   未初始化数据目录的显式旧装配仍允许仅内存工作；
5. 冻结身份（goal_revision/uid/room_id/run_id）随文件保留并继续生效。

全部使用临时数据目录，不联网、不发送真实通知。
"""

import json
import os
import stat
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from port_support.backend_environment import isolated_environment

from app.core.email_sender import EmailSender


class _TokenCase(unittest.TestCase):
    def setUp(self):
        self._env = isolated_environment()
        self.tmp = self._env.__enter__()
        self.addCleanup(lambda: self._env.__exit__(None, None, None))
        self.data_dir = self.tmp / 'data'
        self.token_file = self.data_dir / 'face_verify_tokens.json'

    def sender(self) -> EmailSender:
        sender = EmailSender()
        sender.disable_cloud_host_probe('isolated-desktop-test')
        self.addCleanup(sender.shutdown)
        return sender

    def read_tokens(self):
        return json.loads(self.token_file.read_text(encoding='utf-8'))['tokens']


class ExpiryTests(_TokenCase):
    def test_default_token_has_no_expiry(self):
        sender = self.sender()
        token = sender.issue_face_verify_token(run_id='run-1', room_id=1)

        self.assertNotIn('expires_at', self.read_tokens()[token])
        with mock.patch('app.core.email_sender.time.time',
                        return_value=time.time() + 10 * 365 * 86400):
            self.assertIsNotNone(sender.peek_face_verify_token(token))
            ok, reason = sender.consume_face_verify_token(token, run_id='run-1')
        self.assertTrue(ok, reason)

    def test_explicit_ttl_still_expires(self):
        sender = self.sender()
        token = sender.issue_face_verify_token(run_id='run-1', ttl_seconds=0)
        time.sleep(0.01)
        ok, reason = sender.consume_face_verify_token(token, run_id='run-1')
        self.assertFalse(ok)
        self.assertIn('过期', reason)


class PersistenceTests(_TokenCase):
    def test_tokens_survive_restart_and_are_single_use(self):
        first = self.sender()
        token = first.issue_face_verify_token(run_id='run-7', room_id=99,
                                              epoch=3, goal_revision=42,
                                              uid=123, zone='测试分区')

        second = self.sender()
        info = second.peek_face_verify_token(token)
        self.assertIsNotNone(info)
        self.assertEqual(info['goal_revision'], 42)
        self.assertEqual(info['uid'], 123)
        ok, reason, _info = second.consume_face_verify_token_ex(token)
        self.assertTrue(ok, reason)

        third = self.sender()
        ok, reason = third.consume_face_verify_token(token)
        self.assertFalse(ok)
        self.assertIn('不存在或已被使用', reason)

    def test_token_file_mode_and_consumed_token_removed(self):
        sender = self.sender()
        token = sender.issue_face_verify_token(run_id='run-8')
        # chmod permission bits are meaningful evidence on POSIX only;
        # stat.S_IMODE does not establish Windows ACL privacy.
        if os.name == 'posix':
            mode = stat.S_IMODE(self.token_file.stat().st_mode)
            self.assertEqual(mode, 0o600)
        self.assertIn(token, self.read_tokens())

        ok, reason = sender.consume_face_verify_token(token)
        self.assertTrue(ok, reason)
        self.assertNotIn(token, self.read_tokens())

    def test_corrupt_file_is_recovered(self):
        self.token_file.write_text('{not json', encoding='utf-8')
        sender = self.sender()
        token = sender.issue_face_verify_token(run_id='run-9')
        self.assertIn(token, self.read_tokens())

    def test_issue_persistence_failure_keeps_notice_but_omits_confirm_link(self):
        sender = self.sender()
        with mock.patch.object(sender, '_persist_tokens_locked',
                               return_value=False), \
                mock.patch.object(sender, '_get_server_host',
                                  return_value='live.example.invalid'), \
                mock.patch.object(sender, 'notify_event',
                                  return_value='queued') as notify:
            result = sender.send_face_verify(
                'https://www.bilibili.com/synthetic-face-check',
                run_id='run-10', room_id=99, goal_revision=42, uid=123)

        self.assertEqual(result, 'queued')
        notify.assert_called_once()
        body = notify.call_args.args[2]
        self.assertIn('邮件确认链接暂不可用', body)
        self.assertIn('验证后请在面板点击“验证完成”', body)
        self.assertNotIn('/api/email/confirm-face-verify?token=', body)
        self.assertFalse(self.token_file.exists())

    def test_failed_consume_write_keeps_token_and_skips_control_until_retry(self):
        """失败落盘不确认消费；重试成功后重启也不能重放原 token。"""
        from app.api.email import _do_confirm

        sender = self.sender()
        token = sender.issue_face_verify_token(
            run_id='run-10b', room_id=99, goal_revision=42, uid=123)
        control = mock.Mock()
        control.confirm.return_value = SimpleNamespace(
            accepted=True, status='accepted', reason='')

        with mock.patch('app.api.email.get_email_sender', return_value=sender), \
                mock.patch('app.api.email.get_control_service',
                           return_value=control), \
                mock.patch('app.core.email_sender.os.replace',
                           side_effect=OSError('simulated token-store failure')):
            ok, message = _do_confirm(token)

        self.assertFalse(ok, message)
        control.confirm.assert_not_called()
        self.assertIn(token, self.read_tokens())

        with mock.patch('app.api.email.get_email_sender', return_value=sender), \
                mock.patch('app.api.email.get_control_service',
                           return_value=control):
            ok, message = _do_confirm(token)
        self.assertTrue(ok, message)
        control.confirm.assert_called_once_with(
            f'email-face:{token}', goal_revision=42, uid=123, room_id=99)
        self.assertNotIn(token, self.read_tokens())

        restarted = self.sender()
        ok, reason, info = restarted.consume_face_verify_token_ex(token)
        self.assertFalse(ok)
        self.assertIn('不存在或已被使用', reason)
        self.assertIsNone(info)

    def test_no_config_data_dir_works_in_memory_only(self):
        with mock.patch('app.core.config.data_dir_initialized',
                        return_value=False):
            sender = self.sender()
            token = sender.issue_face_verify_token(run_id='run-11')
            ok, reason = sender.consume_face_verify_token(token)
        self.assertTrue(ok, reason)
        self.assertFalse(self.token_file.exists())

    def test_frozen_identity_survives_restart_and_is_enforced(self):
        first = self.sender()
        token = first.issue_face_verify_token(run_id='run-12', room_id=77,
                                              goal_revision=5, uid=9)
        second = self.sender()
        info = second.peek_face_verify_token(token)
        self.assertEqual((info['goal_revision'], info['uid'], info['room_id']),
                         (5, 9, 77))
        ok, reason = second.consume_face_verify_token(
            token, run_id='run-12', room_id=77)
        self.assertTrue(ok, reason)

        other = second.issue_face_verify_token(run_id='run-13', room_id=77)
        third = self.sender()
        ok, reason = third.consume_face_verify_token(other, room_id=78)
        self.assertFalse(ok)
        self.assertIn('直播间', reason)


if __name__ == '__main__':
    unittest.main()
