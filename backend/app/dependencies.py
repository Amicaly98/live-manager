"""
dependencies.py - 依赖注入（全局单例持有者）

解决 app/main.py 和 app/api/*.py 之间的循环导入问题。
所有 API 模块通过此文件获取 task_manager / live_controller 实例。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ==================== 全局单例容器 ====================
_task_manager = None
_live_controller = None
_email_sender = None


def init_globals(task_manager, live_controller, email_sender=None):
    """初始化全局单例（由 lifespan 调用）"""
    global _task_manager, _live_controller, _email_sender
    _task_manager = task_manager
    _live_controller = live_controller
    _email_sender = email_sender
    logger.info("全局依赖已初始化")


def shutdown_globals():
    """清理全局单例（由 lifespan 调用）"""
    global _task_manager, _live_controller, _email_sender
    logger.info("正在关闭全局依赖...")
    if _email_sender:
        try:
            _email_sender.shutdown()
        except Exception as e:
            logger.error(f"邮件发送器关闭异常: {e}")
    if _live_controller:
        try:
            _live_controller.shutdown()
        except Exception as e:
            logger.error(f"直播控制器关闭异常: {e}")
    if _task_manager:
        try:
            _task_manager.shutdown()
        except Exception as e:
            logger.error(f"任务管理器关闭异常: {e}")
    _task_manager = None
    _live_controller = None
    _email_sender = None
    logger.info("全局依赖已清理")


def get_task_manager():
    """获取任务管理器实例"""
    return _task_manager


def get_live_controller():
    """获取直播控制器实例"""
    return _live_controller


def get_email_sender():
    """获取邮件发送器实例"""
    return _email_sender
