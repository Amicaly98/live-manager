"""在**单个进程内**针对某一个产品运行共同契约场景。

用法（两端必须分别启动独立进程——两个仓库的 app 包名相同，不能同进程混用）：

    python run_contracts.py --product server  --json ../../deliveries/cross-repo-alignment/contract-results-server.json
    python run_contracts.py --product desktop --json ../../deliveries/cross-repo-alignment/contract-results-desktop.json

退出码：0=全部通过；1=存在失败。没有"环境跳过"档位：构造失败按失败记录。
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--product', choices=['server', 'desktop'], required=True)
    ap.add_argument('--json', default='', help='结果 JSON 输出路径')
    ap.add_argument('--data-dir', default='', help='桌面端隔离数据目录（必须先于导入设置）')
    ap.add_argument('--only', default='', help='只跑指定场景 ID（逗号分隔）')
    args = ap.parse_args()

    if args.product == 'desktop':
        data_dir = args.data_dir or tempfile.mkdtemp(prefix='contract-desktop-')
        # 必须在 import app.core.config 之前设置数据目录
        os.environ['BILIBILI_DATA_DIR'] = data_dir
        os.environ.setdefault('BILIBILI_SKIP_MIGRATION', '1')
        from adapter_desktop import DesktopAdapter
        adapter = DesktopAdapter()
    else:
        from adapter_server import ServerAdapter
        adapter = ServerAdapter()

    import contract_core
    scenarios = list(contract_core.SCENARIOS)
    if args.only:
        wanted = {x.strip() for x in args.only.split(',') if x.strip()}
        scenarios = [s for s in scenarios if s.sid in wanted]

    results = []
    print(f'=== product={args.product} repo={HERE.parent} scenarios={len(scenarios)} ===')
    for sc in scenarios:
        ok, error = True, ''
        try:
            sc.fn(adapter)
        except Exception as exc:
            ok, error = False, f'{type(exc).__name__}: {exc}'
        results.append({'id': sc.sid, 'group': sc.group, 'title': sc.title,
                        'ok': ok, 'error': error})
        print(f'{"PASS" if ok else "FAIL"}  {sc.sid:<12} {sc.group:<9} {sc.title}')
        if not ok:
            print(f'      -> {error}')

    failed = [r for r in results if not r['ok']]
    print(f'--- {len(results) - len(failed)}/{len(results)} passed ---')
    payload = {
        'product': args.product,
        'adapter_caps': getattr(adapter, 'CAPS', {}),
        'ran_at': datetime.now().isoformat(timespec='seconds'),
        'total': len(results),
        'passed': len(results) - len(failed),
        'failed': len(failed),
        'results': results,
    }
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        print(f'json -> {out}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
