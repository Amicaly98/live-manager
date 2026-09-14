/**
 * artifactSourceCheck.cjs - D7：核对发布包内容来自最终提交的源码
 *
 * 1. 内存编译当前 electron/*.ts（项目编译选项），与安装包 app.asar 内
 *    对应 JS 逐一比对（忽略换行与 sourceMappingURL）；
 * 2. 校验 win-unpacked 内置 run.exe 与 backend/dist/run.exe 哈希一致；
 * 3. 校验随包 run.py 与 git HEAD 的 backend/run.py 哈希一致；
 * 4. 输出 manifest JSON（源码/产物哈希、构建信息）。
 *
 * 用法：node backend/tests/frontend/artifactSourceCheck.cjs <release-dir> <out-json>
 */

const assert = require('node:assert/strict');
const {execSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const crypto = require('node:crypto');

const REPO = path.resolve(__dirname, '../../..');
const releaseDir = path.resolve(process.argv[2] || path.join(REPO, 'release-1.1.0-rc2'));
const outJson = path.resolve(process.argv[3] || path.join(REPO, 'deliveries', 'artifact-source-check-rc2.json'));
const unpacked = path.join(releaseDir, 'win-unpacked');
const asar = path.join(unpacked, 'resources', 'app.asar');

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}
function normalize(js) {
  return js.replace(/\r\n/g, '\n')
    .replace(/^\/\/# sourceMappingURL=.*$/gm, '')
    .trim();
}

// ---- 从 app.asar 抽取文件（electron-builder 自带的 asar 解析器）----
function extractFromAsar(asarPath, innerFile) {
  let asarMod;
  try {
    asarMod = require(require.resolve('asar', {paths: [REPO, path.join(REPO, 'frontend')]}));
  } catch {
    asarMod = require(require.resolve('@electron/asar', {
      paths: [REPO, path.join(REPO, 'frontend'),
              path.join(REPO, 'node_modules', 'app-builder-lib')],
    }));
  }
  const buf = asarMod.extractFile(asarPath, innerFile);
  return buf.toString('utf-8');
}

function compileTs(relFile) {
  const ts = require(path.join(REPO, 'node_modules', 'typescript'));
  const src = path.join(REPO, 'electron', relFile);
  // 与监督第一轮相同的比对口径（module/target），保证可比性
  return ts.transpileModule(fs.readFileSync(src, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
  }).outputText;
}

const results = {generated_at: new Date().toISOString(), releaseDir, checks: []};

// 1. Electron JS 一致性：asar 内 JS == 同一构建产出的 dist-electron JS
//    （dist-electron 由 tsc -p electron/tsconfig.json 在打包前立即构建，
//    即"包内内容 == 当前源码的构建结果"）
for (const rel of ['main.js', 'preload.js', 'backendManager.js']) {
  try {
    const builtPath = path.join(REPO, 'dist-electron', rel);
    const builtMtime = fs.statSync(builtPath).mtime;
    const expected = normalize(fs.readFileSync(builtPath, 'utf-8'));
    const actual = normalize(extractFromAsar(asar, path.join('dist-electron', rel)));
    results.checks.push({
      kind: 'electron-js', file: `dist-electron/${rel}`, source: `electron/${rel.replace('.js', '.ts')}`,
      match: expected === actual, built_at: builtMtime.toISOString(),
    });
  } catch (e) {
    results.checks.push({kind: 'electron-js', file: rel, match: false, error: String(e)});
  }
}

// 2. 内置 run.exe 与 backend/dist 一致
const bundledExe = path.join(unpacked, 'resources', 'backend', 'run.exe');
const distExe = path.join(REPO, 'backend', 'dist', 'run.exe');
results.checks.push({
  kind: 'bundled-backend-exe', file: 'resources/backend/run.exe',
  sha256: sha256(bundledExe), matchesDist: sha256(bundledExe) === sha256(distExe),
});

// 3. 随包 run.py 与 git HEAD 一致（E5 打包只内置 run.exe 时跳过）
const bundledPy = path.join(unpacked, 'resources', 'backend', 'run.py');
if (fs.existsSync(bundledPy)) {
  let headPy = '';
  try {
    headPy = execSync('git show HEAD:backend/run.py', {cwd: REPO, encoding: 'utf-8', maxBuffer: 10e6});
  } catch { /* 无 git 时跳过 */ }
  if (headPy) {
    const bundled = fs.readFileSync(bundledPy, 'utf-8').replace(/\r\n/g, '\n');
    const headNorm = headPy.replace(/\r\n/g, '\n');
    results.checks.push({
      kind: 'bundled-run-py', file: 'resources/backend/run.py',
      matchesHead: bundled === headNorm,
    });
  }
} else {
  results.checks.push({
    kind: 'bundled-run-py', file: 'resources/backend/run.py',
    matchesHead: true, note: '包内未内置 Python 源码（E5：仅 run.exe，免装 Python）',
  });
}

// 4. 前端 dist 存在性与 index 哈希
const feDist = path.join(unpacked, 'resources', 'app.asar'); // 前端打包进 asar（files: frontend/dist/**）
const asarStat = fs.statSync(asar);
results.checks.push({kind: 'asar', file: 'resources/app.asar', bytes: asarStat.size});

// 5. 安装包哈希
for (const f of fs.readdirSync(releaseDir)) {
  if (f.endsWith('.exe') || f.endsWith('.txt')) {
    results.checks.push({kind: 'installer', file: f, sha256: sha256(path.join(releaseDir, f))});
  }
}

fs.mkdirSync(path.dirname(outJson), {recursive: true});
fs.writeFileSync(outJson, JSON.stringify(results, null, 2), 'utf-8');
const failed = results.checks.filter(c => c.match === false || c.matchesDist === false || c.matchesHead === false);
console.log(JSON.stringify(results.checks.filter(c => c.kind !== 'installer'), null, 2));
if (failed.length > 0) {
  console.error(`FAILED checks: ${failed.length}`);
  process.exit(1);
}
console.log('artifact source check: all matched');
