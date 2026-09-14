"""契约工具负向自测：证明工具本身不会"假 PASS"。

用法（与 run_contracts.py 同一进程规范）：

    python tool_selfcheck.py --product server
    python tool_selfcheck.py --product desktop --data-dir <隔离数据目录>

检查项（共 **13** 条断言，全部必须成立；任何一条不成立即非零退出）：
1. 受控交错 worker 抛异常时，``interleaved_worker`` 必须把异常回传到主线程
   （只凭"线程已结束"不得判通过）；
2. ``--only`` 指定不存在的场景 ID → 非零退出，且报错点名该 ID；
3. ``--only`` 混合"存在 + 不存在" → 非零退出（不得只跑存在的部分）；
4. ``--only`` 解析出 0 个场景 → 非零退出（不得把 0/0 当通过）；
5. 工具/选择错误也要写结果 JSON，且含被测快照字段；
6. 场景失败时结果 JSON 仍然写出，且带**有效**被测快照（真实提交格式）；
7. 有效运行时 head_commit 等于真实 ``rev-parse HEAD`` 且为 40 位 hex；
8. 有效运行时适配器/共享场景文件哈希等于磁盘现算值（且不含 (missing)）；
9. 有效运行时工作区差异哈希与现算 sha256(status+diff) 自洽；
10. git ``safe.directory`` 只针对本仓的**规范化绝对路径**（不得含反斜杠、
    不得用 ``*`` 通配）；
11. 受控 Git 失败（把仓库根指向非 git 目录）→ 快照必须被判无效：
    ``snapshot_valid=False``、``head_commit`` 为空、``worktree_diff_sha256`` 为空、
    原始错误进 ``snapshot_error``；**不能**把错误文本（或其哈希）当成有效来源；
12. 发布验收路径（``--require-valid-snapshot``）：行为场景全通过但快照无效时返回
    3（不可验收），同时仍然保存实际场景结果与原始错误；
13. CLI 开关在快照有效时不阻断（rc=0 且 snapshot_valid=True）。

注：日志里会出现一条 ``FAIL SELFTEST-FAIL``，那是**故意失败**的场景，用于验证
"失败运行仍保存有效快照"（第 6 条），不是自测失败。
"""

import argparse
import hashlib
import json
import os
import re
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
HEAD_RE = re.compile(r'^[0-9a-f]{40}$')


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


def _run_cli(product, data_dir, only, json_path, extra_args=()):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    cmd = [sys.executable, str(HERE / 'run_contracts.py'), '--product', product,
           '--only', only, '--json', str(json_path), *extra_args]
    if data_dir:
        cmd += ['--data-dir', data_dir]
    out = subprocess.run(cmd, cwd=str(HERE), env=env, capture_output=True,
                         text=True, encoding='utf-8', errors='replace', timeout=900)
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
    """5) 失败运行同样保存结果，且内嵌**有效**被测快照。"""
    sc = contract_core.Scenario(
        'SELFTEST-FAIL', 'SELFTEST', '工具自测：故意失败的场景',
        lambda a: (_ for _ in ()).throw(AssertionError('selftest-intentional')))
    out = tmp / 'selftest-fail.json'
    rc = run_contracts.run_selected(adapter, [sc], product,
                                    json_path=str(out), only='SELFTEST-FAIL')
    payload = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {}
    snapshot = payload.get('source_snapshot') or {}
    head = str(snapshot.get('head_commit') or '')
    ok = (rc == 1 and payload.get('failed') == 1 and out.exists()
          and snapshot.get('snapshot_valid') is True
          and bool(HEAD_RE.match(head))
          and len(str(snapshot.get('worktree_diff_sha256') or '')) == 64
          and bool(snapshot.get('files')))
    check('失败运行保存 JSON + 有效被测快照', ok,
          f"rc={rc} failed={payload.get('failed')} head={head[:12]} "
          f"valid={snapshot.get('snapshot_valid')}")


def check_valid_snapshot_provenance(adapter, product, tmp):
    """6) 有效运行：快照确实归属于本仓（真实提交格式 + 现算哈希一致）。"""
    sc = contract_core.Scenario('SELFTEST-OK', 'SELFTEST',
                                '工具自测：必定通过的场景', lambda a: True)
    out = tmp / 'selftest-ok.json'
    rc = run_contracts.run_selected(adapter, [sc], product,
                                    json_path=str(out), only='SELFTEST-OK')
    payload = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {}
    snapshot = payload.get('source_snapshot') or {}

    git_ok, real_head = run_contracts._git_query('rev-parse', 'HEAD')
    real_head = real_head.strip() if git_ok else ''
    ok_head = (rc == 0 and snapshot.get('snapshot_valid') is True
               and HEAD_RE.match(str(snapshot.get('head_commit') or ''))
               and snapshot.get('head_commit') == real_head)
    check('有效运行：head_commit 是真实提交且格式正确', ok_head,
          f"rc={rc} head={str(snapshot.get('head_commit'))[:12]} real={real_head[:12]}")

    files = snapshot.get('files') or {}
    mismatched = []
    for rel, recorded in files.items():
        actual = run_contracts.sha256_file(run_contracts.REPO_ROOT / rel)
        if actual != recorded:
            mismatched.append(rel)
    expect_files = len(run_contracts.SHARED_FILES) + 1   # 共享文件 + 本产品适配器
    ok_files = (len(files) == expect_files and not mismatched
                and all(v and v != '(missing)' for v in files.values()))
    check('有效运行：适配器/共享场景哈希与磁盘一致', ok_files,
          f'files={len(files)}/{expect_files} mismatched={mismatched}')

    status_ok, status = run_contracts._git_query('status', '--porcelain')
    diff_ok, diff = run_contracts._git_query('diff', 'HEAD')
    expect_sha = ''
    if status_ok and diff_ok:
        expect_sha = hashlib.sha256(
            (status + '\n--\n' + diff).encode('utf-8')).hexdigest()
    ok_worktree = bool(expect_sha) and snapshot.get('worktree_diff_sha256') == expect_sha
    check('有效运行：工作区差异哈希自洽', ok_worktree,
          f"recorded={str(snapshot.get('worktree_diff_sha256'))[:12]} "
          f"expect={expect_sha[:12]}")


def check_safe_directory_is_posix():
    """7) git safe.directory 必须是本仓的规范化绝对路径（无通配、无反斜杠）。

    Windows 上 ``str(Path)`` 是反斜杠形式；git 对该目录的 safe.directory 匹配
    会因此失败并以 128 退出（dubious ownership），整套快照查询随之失效。
    """
    arg = run_contracts._safe_directory_arg()
    expected = run_contracts.REPO_ROOT.resolve().as_posix()
    check('safe.directory 使用规范化绝对路径（POSIX 形式、无通配）',
          arg == expected and '\\' not in arg and '*' not in arg
          and Path(arg).is_absolute(),
          f'arg={arg}')


def check_git_failure_is_not_a_snapshot(adapter, product, tmp):
    """8/9) 受控 Git 失败：快照必须被判无效，且不能冒充来源证据。"""
    not_a_repo = tmp / 'not-a-git-repo'
    not_a_repo.mkdir(exist_ok=True)
    saved_root = run_contracts.REPO_ROOT
    saved_dir = run_contracts.CONTRACTS_DIR
    try:
        run_contracts.REPO_ROOT = not_a_repo
        run_contracts.CONTRACTS_DIR = not_a_repo
        snapshot = run_contracts.source_snapshot()
    finally:
        run_contracts.REPO_ROOT = saved_root
        run_contracts.CONTRACTS_DIR = saved_dir

    error_text = str(snapshot.get('snapshot_error') or '')
    head = str(snapshot.get('head_commit') or '')
    diff_sha = str(snapshot.get('worktree_diff_sha256') or '')
    error_sha = hashlib.sha256(error_text.encode('utf-8')).hexdigest()
    ok = (snapshot.get('snapshot_valid') is False
          and str(snapshot.get('snapshot_error') or '') != ''
          and ('git error' in error_text or 'git unavailable' in error_text)
          and head == '' and snapshot.get('head_commit_valid') is False
          and diff_sha == '' and diff_sha != error_sha)
    check('受控 Git 失败：快照被判无效且不冒充来源', ok,
          f"valid={snapshot.get('snapshot_valid')} head={head[:40]!r} "
          f"diff_sha={diff_sha[:12]!r} err={error_text[:60]}")

    # 9) 发布验收路径：行为通过但快照无效 → 非零（不可验收），结果仍保存
    sc = contract_core.Scenario('SELFTEST-OK2', 'SELFTEST',
                                '工具自测：必定通过的场景（快照无效）', lambda a: True)
    out = tmp / 'selftest-invalid-snapshot.json'
    saved_root = run_contracts.REPO_ROOT
    saved_dir = run_contracts.CONTRACTS_DIR
    try:
        run_contracts.REPO_ROOT = not_a_repo
        run_contracts.CONTRACTS_DIR = not_a_repo
        rc = run_contracts.run_selected(adapter, [sc], product, json_path=str(out),
                                        only='SELFTEST-OK2',
                                        require_valid_snapshot=True)
    finally:
        run_contracts.REPO_ROOT = saved_root
        run_contracts.CONTRACTS_DIR = saved_dir
    payload = json.loads(out.read_text(encoding='utf-8')) if out.exists() else {}
    snap2 = payload.get('source_snapshot') or {}
    results = payload.get('results') or []
    ok_gate = (rc == 3 and payload.get('passed') == 1 and payload.get('failed') == 0
               and len(results) == 1 and results[0].get('ok') is True
               and snap2.get('snapshot_valid') is False
               and bool(snap2.get('snapshot_error')))
    check('发布验收：快照无效 → 不可验收（rc=3），行为结果与原始错误都保留', ok_gate,
          f"rc={rc} passed={payload.get('passed')} failed={payload.get('failed')} "
          f"valid={snap2.get('snapshot_valid')}")


def check_cli_requires_valid_snapshot(product, data_dir, tmp):
    """9b) CLI 开关存在且有效运行下不干扰行为结果（rc=0）。"""
    rc, out = _run_cli(product, data_dir, 'CTRL-02a',
                       tmp / 'selftest-cli-gate.json',
                       extra_args=('--require-valid-snapshot',))
    payload_path = tmp / 'selftest-cli-gate.json'
    payload = (json.loads(payload_path.read_text(encoding='utf-8'))
               if payload_path.exists() else {})
    snapshot = payload.get('source_snapshot') or {}
    check('CLI --require-valid-snapshot 在有效快照下不阻断',
          rc == 0 and snapshot.get('snapshot_valid') is True,
          f"rc={rc} valid={snapshot.get('snapshot_valid')}")


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
        check_valid_snapshot_provenance(adapter, args.product, tmp)
        check_safe_directory_is_posix()
        check_git_failure_is_not_a_snapshot(adapter, args.product, tmp)
        check_cli_requires_valid_snapshot(args.product, args.data_dir, tmp)

    print(f'--- {len(FAILURES)} 项自测失败 ---' if FAILURES else '--- 工具自测全部通过 ---')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
