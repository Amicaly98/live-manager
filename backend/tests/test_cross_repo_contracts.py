"""共同契约（双仓镜像）在本仓常规回归里的薄包装。

真正的场景与断言在 ``<repo>/stability-contracts/scenarios/contract_core.py``；
适配器把场景接到本仓真实入口。本文件只负责：定位目录、跑场景、把失败显式列出。

用 unittest.TestCase 编写，因此 pytest 与 ``python -m unittest`` 都能收集。
本文件在两端逐字节一致（按磁盘上存在的适配器自动选择）。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / 'stability-contracts' / 'scenarios'


class CommonContractTests(unittest.TestCase):
    def test_all_common_contract_scenarios_pass(self):
        if str(SCENARIOS_DIR) not in sys.path:
            sys.path.insert(0, str(SCENARIOS_DIR))
        import contract_core

        if (SCENARIOS_DIR / 'adapter_server.py').exists():
            from adapter_server import ServerAdapter as Adapter
        elif (SCENARIOS_DIR / 'adapter_desktop.py').exists():
            from adapter_desktop import DesktopAdapter as Adapter
        else:
            self.fail(f'共同契约适配器缺失：{SCENARIOS_DIR}')

        adapter = Adapter()
        executed, failures = [], []
        for sc in contract_core.SCENARIOS:
            executed.append(sc.sid)
            try:
                sc.fn(adapter)
            except Exception as exc:
                failures.append(f'{sc.sid} [{sc.group}] {sc.title}\n    '
                                f'{type(exc).__name__}: {exc}')

        self.assertTrue(executed, '共同契约场景集为空（不应发生）')
        self.assertFalse(
            failures,
            f'共同契约失败 {len(failures)}/{len(executed)}：\n' + '\n'.join(failures))


if __name__ == '__main__':
    unittest.main()
