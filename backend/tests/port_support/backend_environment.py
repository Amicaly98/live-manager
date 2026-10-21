"""Shared backend regressions with desktop config and isolated synthetic data."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core import config
from app.core.live_controller import BilibiliApi
from app.core.task_manager import TaskManager


@contextmanager
def isolated_environment(zones=(), placeholders=True):
    """Temporarily rebind the desktop's once-initialized paths to a fresh root."""
    with TemporaryDirectory(prefix='desktop-backend-port-') as directory:
        root = Path(directory)
        videos = root / 'videos'
        for zone in [*zones, 'default']:
            folder = videos / zone
            folder.mkdir(parents=True, exist_ok=True)
            if placeholders:
                (folder / 'fixture.mp4').write_bytes(b'fixture-not-video')
        with patch.object(config, '_DATA_DIR', None), \
                patch.object(config, '_data_dir_initialized', False), \
                patch.object(config, 'ACCOUNT_DATA_ROOT', None), \
                patch.object(config, 'VIDEO_BASE_PATH', videos), \
                patch.object(config, 'DEFAULT_VIDEO_FOLDER', videos / 'default'), \
                patch.dict('os.environ', {'VIDEO_BASE_PATH': str(videos),
                                          'ACCOUNT_DATA_ROOT': '',
                                          'AREA_CACHE_PATH': ''}):
            config.init(str(root / 'data'))
            yield root


def build_task_manager(data_dir, tasks):
    """Use real SQLite without sample tasks, Excel import, or legacy reset threads."""
    root = Path(data_dir)
    manager = TaskManager(
        db_path=str(root / 'live_tasks.db'),
        excel_path=str(root / 'live_tasks.xlsx'),
        reset_scheduler=False, seed_sample_data=False, import_from_excel=False,
        last_run_date_file=str(root / 'last-run.json'))
    for task in tasks:
        manager.db.insert_task(task)
    manager.load_tasks()
    return manager


class FakePlatform(BilibiliApi):
    """Only supplied platform responses are allowed; no physical HTTP fallback."""

    def _req(self, method, url, params=None, data=None):
        raise AssertionError(f'isolated platform attempted HTTP: {method} {url}')
