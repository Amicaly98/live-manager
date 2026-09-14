"""在**单个进程内**针对某一个产品运行共同契约场景。

用法（两端必须分别启动独立进程——两个仓库的 app 包名相同，不能同进程混用）：

    python run_contracts.py --product server  --json ../../deliveries/cross-repo-alignment/contract-results-server.json
    python run_contracts.py --product desktop --data-dir <隔离数据目录> --json .../contract-results-desktop.json

退出码：
- 0 = 选择的场景全部通过**且**被测快照有效；
- 1 = 至少一个场景失败（断言失败或环境错误，逐条记录）；
- 2 = 工具/选择错误（未知场景 ID、空选择、适配器构造失败）；
- 3 = 场景通过但被测快照无效，且本次运行要求有效快照（``--require-valid-snapshot``，
  发布验收必须带这个开关）：**行为结果不能替代来源证据**。

没有"环境跳过"档位：构造失败按失败记录。``--only`` 的未知 ID 与 0 场景选择
一律非零退出，绝不把拼错的验收项悄悄丢掉或把"跑 0 个场景"当成通过。

结果 JSON 内嵌**被测快照**（产品源码 HEAD/分支/工作区差异哈希 + 适配器与共享
场景文件哈希），因此旧结果不会被误当成新 HEAD 的证据。失败运行同样写 JSON。

快照有效性是**独立字段**：git 查询失败时 ``snapshot_valid=False``、``head_commit``
留空、``snapshot_error`` 保留原始错误文本——错误文本（及其哈希）绝不会被写进
提交字段冒充来源证据。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACTS_DIR = HERE.parent
REPO_ROOT = CONTRACTS_DIR.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ADAPTER_FILE = {
    'server': 'stability-contracts/scenarios/adapter_server.py',
    'desktop': 'stability-contracts/scenarios/adapter_desktop.py',
}
SHARED_FILES = [
    'stability-contracts/contracts.json',
    'stability-contracts/upstream.json',
    'stability-contracts/scenarios/contract_core.py',
    'stability-contracts/scenarios/run_contracts.py',
]
HEAD_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return '(missing)'


def _safe_directory_arg() -> str:
    """git ``safe.directory`` 的取值：**规范化绝对路径**（正斜杠）。

    Windows 上 ``str(Path)`` 是 ``F:\\...`` 反斜杠形式；git 对 safe.directory
    的目录匹配在部分环境下会因此失败并以 128 退出（dubious ownership），
    使整套快照查询失效。这里统一 ``as_posix()``，并且**只针对本仓目录**：
    不使用 ``*`` 通配信任，也不写入任何全局/用户级 git 配置。
    """
    return REPO_ROOT.resolve().as_posix()


def _git_query(*args):
    """执行 git，返回 ``(ok, text)``。

    失败时 ``text`` 是**原始错误文本**（供记录），``ok=False``——调用方必须
    显式区分，绝不能把错误文本当有效输出。
    """
    try:
        out = subprocess.run(
            ['git', '-c', f'safe.directory={_safe_directory_arg()}',
             '-C', str(REPO_ROOT), *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f'git unavailable: {type(exc).__name__}: {exc}'
    if out.returncode != 0:
        return False, f'git error {out.returncode}: {(out.stderr or "").strip()[:200]}'
    return True, out.stdout


def source_snapshot(product: str = '') -> dict:
    """本仓被测快照：提交、工作区差异哈希、适配器与共享文件哈希。

    Git 查询失败时**不伪造任何有效字段**：``head_commit`` 留空、
    ``head_commit_valid=False``、``worktree_diff_sha256`` 留空、
    ``snapshot_valid=False``，原始错误进 ``snapshot_error``。

    ``product`` 指定时只登记**本产品**的适配器（对端适配器不在本仓，登记成
    ``(missing)`` 只会给证据加噪声）。
    """
    errors = []

    def query(label, *args):
        ok, text = _git_query(*args)
        if not ok:
            errors.append(f'{label}: {text}')
            return None
        return text

    head_raw = query('rev-parse HEAD', 'rev-parse', 'HEAD')
    head = (head_raw or '').strip()
    head_valid = False
    if head_raw is None:
        head = ''
    elif not HEAD_COMMIT_RE.match(head):
        errors.append(f'rev-parse HEAD: 非提交格式 {head[:60]!r}')
        head = ''
    else:
        head_valid = True

    status = query('status --porcelain', 'status', '--porcelain')
    diff = query('diff HEAD', 'diff', 'HEAD')
    branch = query('branch --show-current', 'branch', '--show-current')

    if product in ADAPTER_FILE:
        tracked = SHARED_FILES + [ADAPTER_FILE[product]]
    else:
        tracked = SHARED_FILES + list(ADAPTER_FILE.values())

    worktree_known = status is not None and diff is not None
    worktree_state = (status or '') + '\n--\n' + (diff or '')
    return {
        'repo': REPO_ROOT.name,
        'branch': (branch or '').strip(),
        'head_commit': head if head_valid else '',
        'head_commit_valid': head_valid,
        'worktree_diff_sha256': (hashlib.sha256(worktree_state.encode('utf-8')).hexdigest()
                                 if worktree_known else ''),
        'worktree_dirty': (bool([l for l in status.splitlines() if l.strip()])
                           if status is not None else None),
        'files': {rel: sha256_file(REPO_ROOT / rel) for rel in tracked},
        'snapshot_valid': not errors,
        'snapshot_error': '; '.join(errors),
    }


def contract_set_version() -> str:
    try:
        return json.loads((CONTRACTS_DIR / 'contracts.json').read_text(
            encoding='utf-8'))['contract_set_version']
    except Exception:
        return '(unknown)'


def load_adapter(product: str, data_dir: str = ''):
    if product == 'desktop':
        # 数据目录必须先于导入 app.core.config
        os.environ['BILIBILI_DATA_DIR'] = data_dir or tempfile.mkdtemp(prefix='contract-desktop-')
        os.environ.setdefault('BILIBILI_SKIP_MIGRATION', '1')
        from adapter_desktop import DesktopAdapter
        return DesktopAdapter()
    from adapter_server import ServerAdapter
    return ServerAdapter()


def select_scenarios(only):
    """解析 --only。返回 (scenarios, error)：error 非空表示工具错误（必须非零退出）。

    ``only is None`` = 未提供选择，跑全部场景。
    """
    import contract_core
    all_scenarios = list(contract_core.SCENARIOS)
    if only is None:
        return all_scenarios, ''
    wanted = [x.strip() for x in only.split(',') if x.strip()]
    if not wanted:
        return [], ('--only 未提供任何有效场景 ID：拒绝把 0 个场景当成通过'
                    '（已知 ID：%s）' % ', '.join(s.sid for s in all_scenarios))
    known = {s.sid for s in all_scenarios}
    unknown = [x for x in wanted if x not in known]
    if unknown:
        return [], ('--only 含未知场景 ID：%s（不会静默跳过；已登记的 ID：%s）'
                    % (', '.join(unknown), ', '.join(s.sid for s in all_scenarios)))
    picked = [s for s in all_scenarios if s.sid in set(wanted)]
    if not picked:
        return [], '--only 选择结果为 0 个场景：拒绝当成通过'
    return picked, ''


def write_payload(payload: dict, json_path: str) -> None:
    if not json_path:
        return
    out = Path(json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    print(f'json -> {out}')


def _print_snapshot(snapshot: dict) -> None:
    if snapshot.get('snapshot_valid'):
        head = snapshot.get('head_commit', '')
        print(f"snapshot: valid=True head={head[:12]} "
              f"dirty={snapshot.get('worktree_dirty')}")
    else:
        print('SNAPSHOT-INVALID  快照查询失败：本次运行**不能**作为来源证据')
        print(f"snapshot_error: {snapshot.get('snapshot_error') or '(未记录)'}")


def run_selected(adapter, scenarios, product: str, json_path: str = '',
                 only=None, require_valid_snapshot: bool = False) -> int:
    """执行选定的场景并**总是**写出结果 JSON（含失败运行）。

    ``require_valid_snapshot``：发布验收使用。行为结果与来源证据分开判定——
    场景全通过但快照无效时返回 3（不可验收），同时仍然保存实际场景结果与
    原始快照错误，不抹掉任何一方的信息。
    """
    results = []
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
    snapshot = source_snapshot(product)
    payload = {
        'product': product,
        'adapter_caps': getattr(adapter, 'CAPS', None),
        'ran_at': datetime.now().isoformat(timespec='seconds'),
        'contract_set_version': contract_set_version(),
        'source_snapshot': snapshot,
        'selection': {'only': only, 'scenario_ids': [s.sid for s in scenarios]},
        'total': len(results),
        'passed': len(results) - len(failed),
        'failed': len(failed),
        'results': results,
    }
    print(f'--- {len(results) - len(failed)}/{len(results)} passed ---')
    _print_snapshot(snapshot)
    if require_valid_snapshot and not snapshot.get('snapshot_valid'):
        payload['acceptance_blocked_by_snapshot'] = True
        print('NOT-ACCEPTED  发布验收要求有效快照：本次运行不可作为验收证据')
    write_payload(payload, json_path)
    if require_valid_snapshot and not snapshot.get('snapshot_valid'):
        return 3
    return 1 if failed else 0


def tool_error_payload(product: str, message: str, json_path: str,
                       only=None) -> int:
    payload = {
        'product': product,
        'ran_at': datetime.now().isoformat(timespec='seconds'),
        'contract_set_version': contract_set_version(),
        'source_snapshot': source_snapshot(product),
        'selection': {'only': only, 'scenario_ids': []},
        'total': 0, 'passed': 0, 'failed': 0, 'results': [],
        'tool_error': message,
    }
    print(f'TOOL-ERROR  {message}')
    _print_snapshot(payload['source_snapshot'])
    write_payload(payload, json_path)
    return 2


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--product', choices=['server', 'desktop'], required=True)
    ap.add_argument('--json', default='', help='结果 JSON 输出路径（失败也写）')
    ap.add_argument('--data-dir', default='', help='桌面端隔离数据目录（必须先于导入设置）')
    ap.add_argument('--only', default=None,
                    help='只跑指定场景 ID（逗号分隔）；未知 ID 或空选择将非零退出')
    ap.add_argument('--require-valid-snapshot', action='store_true',
                    help='发布验收：快照无效时以退出码 3 标记不可验收（行为结果仍保存）')
    args = ap.parse_args()

    print(f'=== product={args.product} repo={REPO_ROOT.name} '
          f'contract_set_version={contract_set_version()} ===')
    scenarios, selection_error = select_scenarios(args.only)
    if selection_error:
        return tool_error_payload(args.product, selection_error, args.json,
                                  only=args.only)

    try:
        adapter = load_adapter(args.product, args.data_dir)
    except Exception as exc:
        return tool_error_payload(
            args.product, f'适配器构造失败（环境/布局问题）：{type(exc).__name__}: {exc}',
            args.json, only=args.only)

    return run_selected(adapter, scenarios, args.product, json_path=args.json,
                        only=args.only,
                        require_valid_snapshot=args.require_valid_snapshot)


if __name__ == '__main__':
    sys.exit(main())
