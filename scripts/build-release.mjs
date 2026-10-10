#!/usr/bin/env node
/**
 * build-release.mjs - 正式构建入口（桌面移植阶段 3）
 *
 * 这一步存在的理由：**打包必须带本轮新构建的 run.exe**。
 * 旧入口 `npm run build` 只做「前端 → Electron」，`backend/dist/run.exe` 是上一次
 * 单独构建 `build:backend` 时留下的产物——不重建后端就打包，会把新页面配上旧后端，
 * 而且没有任何提示。这里把三件事按**固定顺序**串起来，前一步失败立即停下：
 *
 *   1) 后端冻结程序   pyinstaller run.spec → backend/dist/run.exe
 *   2) 前端           vite build          → frontend/dist/
 *   3) Electron       tsc + 图标 + electron-builder（win-unpacked / NSIS / portable）
 *                     → release/（--publish never：只生成包，绝不发布）
 *
 * 每一步都先删掉上一轮的产物再重建（残留文件会让"构建成功"变成假象：
 * 进程崩了但文件还在，看起来就像构建过了），构建完再核对
 * `release/win-unpacked/resources/backend/run.exe` 与**本次**构建的 run.exe 逐字节一致。
 *
 * 构建输入（分区种子）沿用仓库既有规则，见 backend/run.spec 顶部注释：
 *   - `BILIBILI_AREA_SEED_FILE` 已设置 → 用它（缺文件/结构非法会让构建立刻失败）；
 *   - 未设置 → 若 `backend/build-inputs/bili_areas_full.json` 存在就用它（本机既有
 *     构建输入，未受跟踪），否则**不含种子**照旧构建成功（首启显示"分区待获取"）。
 *     不从开发机的运行数据目录里随便捞一个分区表。
 *
 * 环境：
 *   - Node：默认用 `tools/node-v22.20.0-win-x64/node.exe`（本机已确认可用），
 *     可用 `BUILD_NODE` 覆盖；
 *   - Python：默认用 `F:/python/python.exe`（本机带全量后端依赖 + PyInstaller），
 *     可用 `BUILD_PYTHON` 覆盖。
 *   两者都会连同版本写进构建清单，便于核对"包是这个环境产出的"。
 *
 * 用法：
 *   `npm run build`                    全链（= node scripts/build-release.mjs）
 *   `npm run build:backend`            只重建后端冻结程序（同一份环境解析与清场）
 *   `node scripts/build-release.mjs --only=frontend`   只重建前端
 */

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SCRIPT_DIR, '..');

// --only=backend|frontend|electron（逗号分隔）；省略 = 全链。
// 无论裁到哪几步，环境解析、"先清场再构建"和"失败即停"都共用同一份实现。
const ONLY = new Set((process.argv.slice(2).join(',') || '')
  .split(',').map((s) => s.trim().replace(/^--only=/, '')).filter(Boolean));
const want = (step) => ONLY.size === 0 || ONLY.has(step);

const DEFAULT_NODE22 = 'F:/livestream/tools/node-v22.20.0-win-x64/node.exe';
const DEFAULT_PYTHON = 'F:/python/python.exe';
const SEED_LOCAL = path.join(ROOT, 'backend', 'build-inputs', 'bili_areas_full.json');
const LOG_DIR = path.join(ROOT, 'release');
const LOG_FILE = path.join(LOG_DIR, 'build-release.log');
const MANIFEST_FILE = path.join(LOG_DIR, 'build-manifest.json');

const RUN_EXE = path.join(ROOT, 'backend', 'dist', 'run.exe');
const PACKAGED_RUN_EXE = path.join(ROOT, 'release', 'win-unpacked', 'resources', 'backend', 'run.exe');
// 版本号沿用现有项目规则：package.json 的 version（electron-builder 也读它，
// 本脚本不另外发明版本）。
const APP_VERSION = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8')).version;
const INSTALLER = path.join(ROOT, 'release', `直播控制系统 Setup ${APP_VERSION}.exe`);
const PORTABLE = path.join(ROOT, 'release', `直播控制系统 ${APP_VERSION}.exe`);

const logLines = [];
function log(msg) {
  const line = String(msg);
  logLines.push(line);
  console.log(line);
}
function fail(msg) {
  log(`[FAIL] ${msg}`);
  flushLog();
  process.exit(1);
}
function flushLog() {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(LOG_FILE, logLines.join('\n') + '\n', 'utf8');
  } catch { /* 日志写不进去不是构建失败的理由 */ }
}

function sha256(file) {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(file));
  return h.digest('hex');
}

/** 删除文件；删不掉就报错（绝不在"可能还有旧产物"的前提下继续）。 */
function removeStale(file, label) {
  if (!fs.existsSync(file)) return;
  try {
    fs.unlinkSync(file);
  } catch (e) {
    fail(`无法删除上一轮遗留的${label}（${file}）：${e.message}；` +
         '构建必须从干净状态开始，请先手动结束占用它的进程');
  }
  if (fs.existsSync(file)) fail(`删除后${label}仍然存在：${file}`);
  log(`[clean] 已移除上一轮${label}：${file}`);
}

/** 运行子进程，输出同时进控制台与构建日志；非零退出即失败。 */
function run(label, cmd, args, opts = {}) {
  log(`\n$ ${label}`);
  log(`  ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, {
    cwd: opts.cwd || ROOT,
    env: opts.env || process.env,
    stdio: opts.capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
    encoding: 'utf8',
    windowsHide: true,
    shell: false,
  });
  if (opts.capture) {
    if (r.stdout) log(r.stdout.trimEnd());
    if (r.stderr) log(r.stderr.trimEnd());
  }
  if (r.error) fail(`${label} 无法启动：${r.error.message}`);
  if (r.status !== 0) fail(`${label} 失败（exit=${r.status}）——后续步骤不再执行`);
  log(`  [ok] ${label} (exit=0)`);
  return r;
}

/** 带 --prefix 的 npm 调用：用指定 node 跑指定 npm-cli.js，避免 PATH 上的版本漂移。 */
function npm(nodeExe, args, opts = {}) {
  const npmCli = path.join(path.dirname(nodeExe), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  if (!fs.existsSync(npmCli)) fail(`找不到 npm-cli.js：${npmCli}`);
  return run(`npm ${args.join(' ')}`, nodeExe, [npmCli, ...args], opts);
}

function main() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  log('='.repeat(72));
  log(`构建开始：${new Date().toISOString()}`);
  log('='.repeat(72));

  // ---------- 环境 ----------
  const nodeExe = process.env.BUILD_NODE || (fs.existsSync(DEFAULT_NODE22) ? DEFAULT_NODE22 : process.execPath);
  if (!fs.existsSync(nodeExe)) fail(`构建用 Node 不存在：${nodeExe}`);
  const pyExe = process.env.BUILD_PYTHON || DEFAULT_PYTHON;
  if (!fs.existsSync(pyExe)) fail(`构建用 Python 不存在：${pyExe}`);

  const nodeVer = String(spawnSync(nodeExe, ['-v'], { encoding: 'utf8' }).stdout || '').trim();
  const pyVer = String(spawnSync(pyExe, ['-c', 'import sys;print(sys.version.split()[0])'],
    { encoding: 'utf8' }).stdout || '').trim();
  const piVer = String(spawnSync(pyExe, ['-c', 'import PyInstaller;print(PyInstaller.__version__)'],
    { encoding: 'utf8' }).stdout || '').trim();
  log(`[env] node   = ${nodeExe} (${nodeVer})`);
  log(`[env] python = ${pyExe} (${pyVer}, PyInstaller ${piVer})`);

  // ---------- 源码提交 ----------
  const git = (args) => String(spawnSync('git', args, { cwd: ROOT, encoding: 'utf8' }).stdout || '').trim();
  const commit = git(['rev-parse', 'HEAD']);
  const branch = git(['rev-parse', '--abbrev-ref', 'HEAD']);
  const dirty = git(['status', '--porcelain']).length > 0;
  log(`[src] branch=${branch} commit=${commit} dirty=${dirty}`);

  // ---------- 分区种子（构建输入） ----------
  const seedEnv = { ...process.env };
  let seedSource = process.env.BILIBILI_AREA_SEED_FILE || '';
  if (!seedSource && fs.existsSync(SEED_LOCAL)) {
    seedSource = SEED_LOCAL;
    seedEnv.BILIBILI_AREA_SEED_FILE = SEED_LOCAL;
  }
  if (seedSource) {
    if (!fs.existsSync(seedSource)) fail(`分区种子不存在：${seedSource}`);
    log(`[seed] 构建输入 = ${seedSource} (sha256=${sha256(seedSource)})`);
  } else {
    log('[seed] 未提供分区种子：本次构建不含内置分区数据（沿用既有行为，非错误）');
  }

  const manifest = {
    built_at: new Date().toISOString(),
    branch,
    commit,
    working_tree_dirty: dirty,
    only: [...ONLY],
    node: { path: nodeExe, version: nodeVer },
    python: { path: pyExe, version: pyVer, pyinstaller: piVer },
    area_seed: seedSource ? { path: seedSource, sha256: sha256(seedSource) } : null,
  };

  // ---------- 1) 后端冻结程序 ----------
  if (want('backend')) {
    removeStale(RUN_EXE, '后端产物 run.exe');
    removeStale(PACKAGED_RUN_EXE, '包内后端 run.exe');
    run('pyinstaller run.spec', pyExe,
      ['-m', 'PyInstaller', 'run.spec', '--noconfirm', '--clean',
        '--distpath', 'dist', '--workpath', 'build'],
      { cwd: path.join(ROOT, 'backend'), env: seedEnv });

    if (!fs.existsSync(RUN_EXE)) fail(`后端构建没有产出 ${RUN_EXE}`);
    const runHash = sha256(RUN_EXE);
    const runSize = fs.statSync(RUN_EXE).size;
    log(`[backend] run.exe ${runSize} 字节 sha256=${runHash}`);
    manifest.backend = { path: RUN_EXE, bytes: runSize, sha256: runHash };
  }

  // ---------- 2) 前端 ----------
  if (want('frontend')) {
    npm(nodeExe, ['run', 'build:frontend']);
    const indexHtml = path.join(ROOT, 'frontend', 'dist', 'index.html');
    if (!fs.existsSync(indexHtml)) fail(`前端构建没有产出 ${indexHtml}`);
  }

  // ---------- 3) Electron 编译 + 打包（--publish never 在 build:electron 里） ----------
  if (want('electron')) {
    npm(nodeExe, ['run', 'build:electron']);
  }

  // ---------- 绑定核对：只有全链构建才谈得上"包内后端=本次构建" ----------
  if (!want('backend') || !want('electron')) {
    log('\n[skip] 非全链构建：不做"包内 run.exe = 本次构建"核对');
    flushLog();
    return;
  }
  if (!fs.existsSync(PACKAGED_RUN_EXE)) fail(`包内没有后端：${PACKAGED_RUN_EXE}`);
  const packagedHash = sha256(PACKAGED_RUN_EXE);
  const runHash = manifest.backend.sha256;
  if (packagedHash !== runHash) {
    fail(`包内 run.exe 与本次构建不一致：本次 ${runHash} / 包内 ${packagedHash}`
      + '——安装包带的是别的后端，不能交付');
  }
  log(`[verify] 包内 run.exe 与本次构建逐字节一致 (sha256=${packagedHash})`);

  for (const [label, file] of [['安装版(NSIS)', INSTALLER], ['便携版(portable)', PORTABLE]]) {
    if (!fs.existsSync(file)) fail(`打包没有产出${label}：${file}`);
  }

  manifest.packaged_backend = { path: PACKAGED_RUN_EXE, sha256: packagedHash };
  manifest.artifacts = [INSTALLER, PORTABLE].map((f) => ({
    path: f, bytes: fs.statSync(f).size, sha256: sha256(f),
  }));
  fs.writeFileSync(MANIFEST_FILE, JSON.stringify(manifest, null, 2), 'utf8');

  log('\n' + '='.repeat(72));
  log('构建完成');
  for (const a of manifest.artifacts) {
    log(`  ${path.basename(a.path)}  ${a.bytes} 字节  sha256=${a.sha256}`);
  }
  log(`  清单：${MANIFEST_FILE}`);
  log('='.repeat(72));
  flushLog();
}

main();
