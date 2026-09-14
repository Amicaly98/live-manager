"""契约工具负向自测：证明工具本身不会"假 PASS"。

用法（与 run_contracts.py 同一进程规范）：

    python tool_selfcheck.py --product server
    python tool_selfcheck.py --product desktop --data-dir <隔离数据目录>

检查项（全部必须成立；任何一项不成立即非零退出）：
1. 受控交错 worker 抛异常时，``interleaved_worker`` 必须把异常回传到主线程
   （只凭"线程已结束"不得判通过）；
2. ``--only`` 指定不存在的场景 ID → 非零退出，且报错点名该 ID；
3. ``--only`` 混合"存在 + 不存在" → 非零退出（不得只跑存在的部分）；
4. ``--only`` 解析出 0 个场景 → 非零退出（不得把 0/0 当通过）；
5. 场景失败时结果 JSON 仍然写出，且带被测快照（HEAD/工作区/适配器/共享文件哈希）。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract_core  # noqa: E402
import run_contracts  # noqa: E402

FAILURES = []


def check(name, condition, detail=''):
    print(f'{"PASS" if condition else "FAIL"}  {name}' + (f'  {detail}' if detail else ''))
    if not condition:
        FAILURES.append(name)


def check_worker_exception_propagation():
    """1) worker 内部异常必须回传主线程，不得被当成"线程结束=通过"。"""
    entered = threading.Event()
    release = threading.Event()

    def boom():
        entered.set()
        release.wait(5)
        raise RuntimeError('selftest-worker-boom')

    captured = ''
    try:
        with contract_core.interleaved_worker(boom, (), entered, release, timeout=5):
            assert entered.wait(5), 'worker 未进入等待点'
        captured = '(未抛错：异常被吞掉)'
    except AssertionError as exc:
        captured = str(exc)
    check('worker 异常回传主线程',
          'worker 异常未回传' in captured and 'selftest-worker-boom' in captured,
          captured[:80])


def _run_cli(product, data_dir, only, json_path):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    cmd = [sys.executable, str(HERE / 'run_contracts.py'), '--product', product,
           '--only', only, '--json', str(json_path)]
    if data_dir:
        cmd += ['--data-dir', data_dir]
    out = subprocess.run(cmd, cwd=str(HERE), env=env, capture_output=True,
                         text=True, encoding='utf-8', errors='replace', timeout=600)
    return out.returncode, (out.stdout or '') + (out.stderr or '')


def check_only_unknown_id(product, data_dir, tmp):
    """2/3/4) --only 的未知 ID、混合 ID、0 场景都必须非零退出。"""
    rc, out = _run_cli(product, data_dir, 'CTRL-THIS-ID-DOES-NOT-EXIST',
                       tmp / 'selftest-unknown.json')
    check('未知场景 ID 非零退出',
          rc == 2 and 'CTRL-THIS-ID-DOES-NOT-EXIST' in out,
          f'rc={rc}')

    rc, out = _run_cli(product, data_dir, 'CTRL-02c,CTRL-NOPE',
                       tmp / 'selftest-mixed.json')
    check('混合(存在+不存在) ID 非零退出',
          rc == 2 and 'CTRL-NOPE' in out,
          f'rc={rc}')

    rc, out = _run_cli(product, data_dir, ' , , ',
                       tmp / 'selftest-empty.json')
    check('0 场景选择非零退出', rc == 2, f'rc={rc}')

    # 工具错误也要留下证据
    payload_path = tmp / 'selftest-unknown.json'
    ok = payload_path.exists()
    detail = ''
    if ok:
        payload = json.loads(payload_path.read_text(encoding='utf-8'))
        ok = bool(payload.get('tool_error')) and 'source_snapshot' in payload
        detail = f"tool_error={str(payload.get('tool_error'))[:40]}"
    check('工具错误也写 JSON（含被测快照）', ok, detail)


def check_failed_run_saves_snapshot(adapter, product, tmp):
    """5) 失败运行同样保存结果，且内嵌被测快照。"""
    contract_core.SCENARIOS.append(contract_core.Scenario(
        'SELFTEST-FAIL', 'SELFTEST', '工具自测：故意失败的场景',
        lambda a: (_ for _ in ()).throw(AssertionError('selftest-intentional'))))
    out = tmp / 'selftest-fail.json'
    rc = run_contracts.run_selected(adapter, [contract_core.SCENARIOS[-1]], product,
                                    json_path=str(out), only='SELFTEST-FAIL')
    payload = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {}
    snapshot = payload.get('source_snapshot') or {}
    ok = (rc == 1 and payload.get('failed') == 1 and out.exists()
          and bool(snapshot.get('head_commit'))
          and bool(snapshot.get('worktree_diff_sha256'))
          and bool(snapshot.get('files')))
    check('失败运行保存 JSON + 被测快照', ok,
          f"rc={rc} failed={payload.get('failed')} head={str(snapshot.get('head_commit'))[:12]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--product', choices=['server', 'desktop'], required=True)
    ap.add_argument('--data-dir', default='', help='桌面端隔离数据目录')
    args = ap.parse_args()

    print(f'=== 契约工具负向自测 product={args.product} ===')
    with tempfile.TemporaryDirectory(prefix='contract-selftest-') as tmpd:
        tmp = Path(tmpd)
        check_worker_exception_propagation()
        adapter = run_contracts.load_adapter(args.product, args.data_dir)
        check_only_unknown_id(args.product, args.data_dir, tmp)
        check_failed_run_saves_snapshot(adapter, args.product, tmp)

    print(f'--- {len(FAILURES)} 项自测失败 ---' if FAILURES else '--- 工具自测全部通过 ---')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
