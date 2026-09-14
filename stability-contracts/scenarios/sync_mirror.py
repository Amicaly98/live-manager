"""契约镜像同步与校验（双仓共用，只处理列明的镜像文件）。

用法（在任一仓库的 stability-contracts/ 下运行）：

    python sync_mirror.py --write-upstream            # 在源仓按当前文件重算 upstream.json
    python sync_mirror.py --check  --other-root <对端仓>   # 校验两端镜像与 upstream.json 一致
    python sync_mirror.py --dry-run --other-root <对端仓>  # 只列两端差异（不做任何写入）
    python sync_mirror.py --verify-source-commit      # 核对 upstream.json 记录的来源提交
                                                      # 确实包含这些内容（git blob 比对）

安全约束：
- 只处理 upstream.json 的 ``mirrored_files`` 列表；**不删除**任何文件、不整目录同步；
- 差异只列出，必须人工确认后再复制；两端按 sha256 逐文件校验。

哈希口径：对**行尾归一化（CRLF→LF）后的文本**取 sha256（``hash_mode`` 字段已记录）。
这样 git 的 autocrlf 检出不会造成"内容相同但哈希不同"的假差异。
备注：``upstream.json`` 是 manifest 自身，不列入 ``mirrored_files``（无法自哈希），
两端一致性由 ``check_alignment.py`` 单独报告。
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
    'stability-contracts/scenarios/tool_selfcheck.py',
    'stability-contracts/scenarios/sync_mirror.py',
    'backend/tests/test_cross_repo_contracts.py',
]
END_LOCAL_FILES = [
    'stability-contracts/scenarios/adapter_server.py',
    'stability-contracts/scenarios/adapter_desktop.py',
]
HASH_MODE = 'text-lf-normalized'


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(path: Path) -> bytes:
    return path.read_bytes().replace(b'\r\n', b'\n')


def sha256(path: Path) -> str:
    return sha256_bytes(normalized(path))


def git(args, cwd):
    return subprocess.check_output(
        ['git', '-c', f'safe.directory={cwd}', '-C', str(cwd), *args],
        text=True, encoding='utf-8', errors='replace').strip()


def load_upstream():
    return json.loads((CONTRACTS_DIR / 'upstream.json').read_text(encoding='utf-8'))


def contract_set_version() -> str:
    try:
        return json.loads((CONTRACTS_DIR / 'contracts.json').read_text(
            encoding='utf-8'))['contract_set_version']
    except Exception:
        return '(unknown)'


def verify_source_commit(payload: dict, print_report: bool = True) -> bool:
    """核对记录的来源提交确实包含这些文件内容（git blob 逐文件比对）。

    做不到核验时**如实返回 False**，调用方应把描述缩窄为"工作区镜像一致"。
    """
    commit = payload.get('contract_commit', '')
    ok = True
    if print_report:
        print(f"来源提交核验：{PROD_ROOT.name}@{commit[:12]}")
    for item in payload['mirrored_files']:
        rel = item['path']
        try:
            blob = subprocess.run(
                ['git', '-c', f'safe.directory={PROD_ROOT}', '-C', str(PROD_ROOT),
                 'show', f'{commit}:{rel}'],
                capture_output=True, check=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            ok = False
            if print_report:
                print(f'  UNVERIFIED {rel}  ({type(exc).__name__})')
            continue
        blob_hash = sha256_bytes(blob.replace(b'\r\n', b'\n'))
        same = ('OK' if blob_hash == item['sha256'] else 'DIFF')
        if same != 'OK':
            ok = False
        if print_report:
            print(f'  {same} {rel}  blob={blob_hash[:16]} manifest={item["sha256"][:16]}')
    if print_report:
        print('来源提交核验通过' if ok else
              '来源提交核验未通过（不得声称"来源提交已校验"）')
    return ok


def write_upstream():
    payload = {
        'schema_version': 2,
        'contract_set_version': contract_set_version(),
        'source_repository': PROD_ROOT.name,
        'source_branch': git(['branch', '--show-current'], PROD_ROOT),
        'contract_commit': git(['rev-parse', 'HEAD'], PROD_ROOT),
        'mirror_targets': ['bilibili-live-manager']
        if PROD_ROOT.name == 'bilibili-live-server' else ['bilibili-live-server'],
        'hash_mode': HASH_MODE,
        'mirrored_files': [
            {'path': rel, 'sha256': sha256(PROD_ROOT / rel)} for rel in MIRRORED_FILES
        ],
        'end_local_files': END_LOCAL_FILES,
        'manifest_files': ['stability-contracts/upstream.json'],
        'sync_rule': (
            '只同步 mirrored_files；先 dry-run 列差异再复制；禁止盲删目录；'
            '两端按 sha256 逐文件校验（行尾归一化）；adapter_* 为端内自持，不参与镜像。'),
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
    print(f"source={up['source_repository']} commit={up['contract_commit']} "
          f"hash_mode={up.get('hash_mode', '(unknown)')}")
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
    if 'upstream.json' not in [i['path'] for i in up['mirrored_files']]:
        dst_manifest = other_root / 'stability-contracts' / 'upstream.json'
        if dst_manifest.exists():
            same = sha256(CONTRACTS_DIR / 'upstream.json') == sha256(dst_manifest)
            print(f'{"OK" if same else "DIFF"} stability-contracts/upstream.json (manifest 自身)')
            if not same:
                bad += 1
        else:
            print('MISSING_DST stability-contracts/upstream.json (manifest 自身)')
            bad += 1
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
    ap.add_argument('--verify-source-commit', action='store_true',
                    help='核对 upstream.json 的来源提交确实包含这些文件内容')
    args = ap.parse_args()

    if args.write_upstream:
        payload = write_upstream()
        verify_source_commit(payload)
        return 0
    if args.verify_source_commit:
        return 0 if verify_source_commit(load_upstream()) else 1
    if not args.other_root:
        print('--check/--dry-run 需要 --other-root', file=sys.stderr)
        return 2
    other = Path(args.other_root)
    if args.check:
        return check(other)
    return dry_run(other)


if __name__ == '__main__':
    sys.exit(main())
