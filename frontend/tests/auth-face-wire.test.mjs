/**
 * auth-face-wire.test.mjs — 认证回包的**头像字段**归一化判据。
 *
 * 背景（现场复核）：`GET /api/auth/status` 与安装回包都带 `face`，但
 * `normalizeLoginPayload` 的 `_userInfo` 只搬 uid/uname/room_id，把头像
 * 字段丢掉了 —— 面板左下角因此永远没有头像。本判据锁死三种形状的搬运：
 *
 *   1) 嵌套形状带 face → 归一化后保留（原样，不改写、不拼接）；
 *   2) 嵌套/平铺没有 face（旧凭据尚未补齐）→ **不写** face 字段，不伪造；
 *   3) 平铺安装回包带 face → 保留。
 *
 * 运行：node tests/auth-face-wire.test.mjs
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..')
const req = createRequire(path.join(repoRoot, 'package.json'))
const ts = req('typescript')

function loadTsModule(relFile) {
  const file = path.join(repoRoot, relFile)
  const source = fs.readFileSync(file, 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
  } }).outputText
  const module = { exports: {} }
  const fn = vm.runInThisContext(`(function(require,module,exports){${code}\n})`,
                                 { filename: file })
  fn(() => { throw new Error('本模块不应有运行时依赖') }, module, module.exports)
  return module.exports
}

const { normalizeLoginPayload } = loadTsModule('src/api/authWire.ts')
const FACE = 'https://i2.hdslb.com/bfs/face/example.jpg'

let failed = 0
function test(name, fn) {
  try {
    fn()
    console.log(`PASS ${name}`)
  } catch (error) {
    failed += 1
    console.error(`FAIL ${name}\n  ${error && error.message}`)
  }
}

test('嵌套形状：face 原样保留', () => {
  const out = normalizeLoginPayload({
    logged_in: true,
    user_info: { uid: 92632991, uname: '77234567', room_id: 22467469,
                 face: FACE },
  })
  assert.equal(out.logged_in, true)
  assert.equal(out.user_info.face, FACE)
})

test('嵌套形状：没有 face 不写字段（不伪造空串）', () => {
  const out = normalizeLoginPayload({
    logged_in: true,
    user_info: { uid: 1, uname: 'U', room_id: 2 },
  })
  assert.equal('face' in out.user_info, false)
})

test('嵌套形状：face 为空串不写字段', () => {
  const out = normalizeLoginPayload({
    logged_in: true,
    user_info: { uid: 1, uname: 'U', room_id: 2, face: '' },
  })
  assert.equal('face' in out.user_info, false)
})

test('平铺安装回包：face 原样保留', () => {
  const out = normalizeLoginPayload({
    logged_in: true, status: 'installed', uid: 9, room_id: 8,
    uname: 'N', face: FACE,
  })
  assert.equal(out.user_info.face, FACE)
  assert.equal(out.user_info.room_id, 8)
})

test('平铺安装回包：缺 face 时其余字段不受影响', () => {
  const out = normalizeLoginPayload({
    logged_in: true, status: 'installed', uid: 9, room_id: 8, uname: 'N',
  })
  assert.equal('face' in out.user_info, false)
  assert.equal(out.user_info.uname, 'N')
})

if (failed > 0) {
  console.error(`\n${failed} 个判据失败`)
  process.exit(1)
}
console.log('\n全部通过（5）')
