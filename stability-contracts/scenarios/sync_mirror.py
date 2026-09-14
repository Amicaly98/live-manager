"""契约镜像同步与校验（双仓共用，只处理列明的镜像文件）。

用法（在任一仓库的 stability-contracts/ 下运行）：

    python sync_mirror.py --write-upstream            # 在源仓按当前文件重算 upstream.json
    python sync_mirror.py --check                     # 校验两端镜像与 upstream.json 一致
    python sync_mirror.py --dry-run                   # 只列出两端镜像文件差异（不做任何写入）

安全约束：
- 只处理 upstream.json 的 ``mirrored_files`` 列表；**不删除**任何文件、不整目录同步；
- 差异只列出，必须人工确认后再复制；两端按 sha256 逐文件校验。
"""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACTS_DIR = HERE.parent
PROD_ROOT = CONTRACTS_DIR.parent

MIRRORED_FILES = [
    'stability-contracts/README.md',
    'stability-contracts/contracts.json',
    'stability-contracts/scenarios/__init__.py',
    'stability-contracts/scenarios/contract_core.py',
    'stability-contracts/scenarios/run_contracts.py',
    'stability-contracts/scenarios/sync_mirror.py',
    'backend/tests/test_cross_repo_contracts.py',
]
END_LOCAL_FILES = [
    'stability-contracts/scenarios/adapter_server.py',
    'stability-contracts/scenarios/adapter_desktop.py',
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git(args, cwd):
    return subprocess.check_output(
        ['git', '-c', f'safe.directory={cwd}', '-C', str(cwd), *args],
        text=True, encoding='utf-8', errors='replace').strip()


def load_upstream():
    return json.loads((CONTRACTS_DIR / 'upstream.json').read_text(encoding='utf-8'))


def write_upstream():
    payload = {
        'schema_version': 1,
        'contract_set_version': '2026-09-14.1',
        'source_repository': PROD_ROOT.name,
        'source_branch': git(['branch', '--show-current'], PROD_ROOT),
        'contract_commit': git(['rev-parse', 'HEAD'], PROD_ROOT),
        'mirror_targets': ['bilibili-live-manager']
        if PROD_ROOT.name == 'bilibili-live-server' else ['bilibili-live-server'],
        'mirrored_files': [
            {'path': rel, 'sha256': sha256(PROD_ROOT / rel)} for rel in MIRRORED_FILES
        ],
        'end_local_files': END_LOCAL_FILES,
        'sync_rule': (
            '只同步 mirrored_files；先 dry-run 列差异再复制；禁止盲删目录；'
            '两端按 sha256 逐文件校验；adapter_* 为端内自持，不参与镜像。'),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }
    (CONTRACTS_DIR / 'upstream.json').write_bytes(
        json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
    print(f"upstream.json 已更新：commit={payload['contract_commit']} "
          f"files={len(payload['mirrored_files'])}")
    return payload


def check(other_root: Path) -> int:
    up = load_upstream()
    bad = 0
    print(f"source={up['source_repository']} commit={up['contract_commit']}")
    for item in up['mirrored_files']:
        rel = item['path']
        src = PROD_ROOT / rel
        dst = other_root / rel
        if not src.exists():
            print(f'MISSING_SRC {rel}')
            bad += 1
            continue
        if not dst.exists():
            print(f'MISSING_DST {rel}')
            bad += 1
            continue
        src_hash = sha256(src)
        dst_hash = sha256(dst)
        state = 'OK' if src_hash == dst_hash == item['sha256'] else 'DIFF'
        if state != 'OK':
            bad += 1
        print(f'{state} {rel}')
        if state == 'DIFF':
            print(f'    upstream={item["sha256"][:16]} src={src_hash[:16]} dst={dst_hash[:16]}')
    print('镜像一致' if bad == 0 else f'镜像不一致：{bad} 项')
    return 1 if bad else 0


def dry_run(other_root: Path) -> int:
    up = load_upstream()
    diffs = 0
    print(f'干运行：只列差异，不写入。source={up["source_repository"]}')
    for item in up['mirrored_files']:
        rel = item['path']
        src = PROD_ROOT / rel
        dst = other_root / rel
        s = sha256(src)[:16] if src.exists() else '(missing)'
        d = sha256(dst)[:16] if dst.exists() else '(missing)'
        mark = 'same' if s == d else 'DIFF'
        if mark != 'same':
            diffs += 1
        print(f'{mark:5} {rel}  src={s} dst={d}')
    extra = []
    other_scen = other_root / 'stability-contracts' / 'scenarios'
    if other_scen.exists():
        known = {Path(x).name for x in MIRRORED_FILES} | {Path(x).name for x in END_LOCAL_FILES}
        for p in sorted(other_scen.iterdir()):
            if p.is_file() and p.name not in known and p.suffix == '.py':
                extra.append(str(p.relative_to(other_root)))
    if extra:
        print('目标仓存在镜像清单之外的文件（不会被自动删除，请人工确认）：')
        for x in extra:
            print(f'  extra {x}')
    print(f'差异 {diffs} 项（未做任何写入）')
    return 1 if diffs else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--other-root', default='', help='对端仓库根目录')
    ap.add_argument('--write-upstream', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.write_upstream:
        write_upstream()
        return 0
    if not args.other_root:
        print('--check/--dry-run 需要 --other-root', file=sys.stderr)
        return 2
    other = Path(args.other_root)
    if args.check:
        return check(other)
    return dry_run(other)


if __name__ == '__main__':
    sys.exit(main())
