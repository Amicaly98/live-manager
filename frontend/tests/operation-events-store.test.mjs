/**
 * 2026-09-21（R1 监督返修）operationEvents store 级有限矩阵。
 *
 * 用**真实的 useCache**与**真实的 operationEvents 源码**（替换浏览器存储与
 * Vue ref，与监督同法），覆盖执行单 R1-4 要求的边界：
 *  1. 真实 useCache 格式的两账号切换（旧缺陷：读外层 uid 永远 anon）；
 *  2. 匿名/登出：缓存移除后回到 anon，新事件不进旧账号命名空间；
 *  3. 自动上下文重载对缺失/已有/畸形目标缓存都整体替换内存；
 *  4. 旧缓存不一致：孤立旧 seen ID 不得压制可信事件补回；
 *  5. 内存 seen 集合有界（300）；
 *  6. 正常同 ID 重复仍只一条、不同 ID 同秒同文案保留（去重不回退）。
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import ts from 'typescript';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = `${path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')}${path.sep}`;
const results = [];
function record(name, ok, detail = '') {
  results.push([name, ok]);
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`);
}

function environment() {
  const values = new Map();
  const storage = {
    getItem: (k) => values.get(k) ?? null,
    setItem: (k, v) => values.set(k, String(v)),
    removeItem: (k) => values.delete(k),
  };
  let seen;
  class ObservedSet extends Set {
    constructor(...args) { super(...args); seen = this; }
  }
  function load(file) {
    const code = fs.readFileSync(root + file, 'utf8');
    const out = vm.runInNewContext(code, {}, { filename: file });
    return out;
  }
  // 用 TypeScript 真实转译（与监督同法）
  function loadTs(file) {
    const compiled = ts.transpileModule(fs.readFileSync(root + file, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const module = { exports: {} };
    vm.runInNewContext(compiled, {
      module, exports: module.exports,
      require: (n) => n === 'vue' ? { ref: (v) => ({ value: v }) } : (() => { throw new Error(n) })(),
      localStorage: storage, Date, Math, Set: ObservedSet, console,
    });
    return module.exports;
  }
  const cache = loadTs('src/composables/useCache.ts').default;
  const events = loadTs('src/stores/operationEvents.ts').operationEvents;
  return { cache, events, values, seen: () => seen };
}

function event(id) {
  return { id, tag: 'TEST', type: 'info', message: id, time: '2026-09-21 12:00:00' };
}

// 1. 真实 useCache 格式两账号切换
{
  const x = environment();
  x.cache.set('user_info', { uid: 101 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('account-A-only')]);
  x.cache.set('user_info', { uid: 202 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('account-B-first')]);   // B 自己的事件
  const keys = [...x.values.keys()].filter((k) => k.startsWith('app_events_v2')).sort();
  const visible = x.events.events.value.map((e) => e.id);
  record('真实缓存协议两账号切换：B 不见 A 的事件，命名空间分开',
    !visible.includes('account-A-only') && visible.includes('account-B-first')
    && keys.includes('app_events_v2:u101') && keys.includes('app_events_v2:u202'),
    JSON.stringify({ visible, keys }));
}

// 2. 匿名/登出
{
  const x = environment();
  x.cache.set('user_info', { uid: 7 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('u7-event')]);
  x.cache.remove('user_info');               // 登出：身份移除
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('anon-event')]);
  const visible = x.events.events.value.map((e) => e.id);
  const keys = [...x.values.keys()].filter((k) => k.startsWith('app_events_v2')).sort();
  record('登出后回到匿名命名空间：u7 的事件不显示，anon 事件独立',
    visible.includes('anon-event') && !visible.includes('u7-event')
    && keys.includes('app_events_v2:u7') && keys.includes('app_events_v2:anon'),
    JSON.stringify({ visible, keys }));
}

// 3a. 缓存到期自动切 anon，目标没有缓存：空摄取也必须清旧内存
{
  const x = environment();
  x.cache.set('user_info', { uid: 55 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('u55-old')]);
  x.cache.set('user_info', { uid: 55 }, -1);   // 立即过期，无 anon 缓存
  x.events.ingestFromStatus([]);
  record('缓存到期→无缓存 anon：无新事件也整体清空旧内存',
    x.events.events.value.length === 0,
    JSON.stringify(x.events.events.value.map((e) => e.id)));
}

// 3b. 匿名身份渐进确认，目标账号没有缓存
{
  const x = environment();
  x.events.ingestFromStatus([event('anon-old')]);
  x.cache.set('user_info', { uid: 88 });
  x.events.ingestFromStatus([]);
  record('anon→无缓存确认账号：无新事件也不保留匿名事件',
    x.events.events.value.length === 0,
    JSON.stringify(x.events.events.value.map((e) => e.id)));
}

// 3c. 自动切换到已有缓存：必须装入目标快照并替换来源快照
{
  const x = environment();
  x.events.ingestFromStatus([event('anon-source')]);
  x.values.set('app_events_v2:u99', JSON.stringify({
    events: [{ ...event('u99-cached'), source: 'server' }],
    seen: ['u99-cached'],
  }));
  x.cache.set('user_info', { uid: 99 });
  x.events.ingestFromStatus([]);
  const visible = x.events.events.value.map((e) => e.id);
  record('自动切换到已有目标缓存：整体装入目标快照',
    visible.length === 1 && visible[0] === 'u99-cached', JSON.stringify(visible));
}

// 3d. 有缓存的账号到无缓存账号：空摄取本身就是重载触发器
{
  const x = environment();
  x.cache.set('user_info', { uid: 1 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('u1-only')]);
  x.cache.set('user_info', { uid: 2 });
  x.events.ingestFromStatus([]);
  record('无新事件的自动重载也替换 events/seen',
    x.events.events.value.length === 0 && x.seen().size === 0,
    `events=${x.events.events.value.length} seen=${x.seen().size}`);
}

// 3e. 合法 JSON 但事件条目畸形：整体视为空，不能发布坏数组或残留 seen
{
  const x = environment();
  x.cache.set('user_info', { uid: 1 });
  x.events.handleContextSwitch();
  x.events.ingestFromStatus([event('u1-old')]);
  x.values.set('app_events_v2:u2', JSON.stringify({ events: [null], seen: ['poison'] }));
  x.cache.set('user_info', { uid: 2 });
  x.events.ingestFromStatus([]);
  record('畸形目标缓存整体降为空',
    x.events.events.value.length === 0 && x.seen().size === 0,
    `events=${x.events.events.value.length} seen=${x.seen().size}`);
}

// 4. 孤立旧 seen ID 不压制可信事件
{
  const x = environment();
  x.values.set('app_events', '[]');
  x.values.set('app_event_ids', JSON.stringify(['lost-event']));
  x.events.ingestFromStatus([event('lost-event')]);
  record('孤立旧 seen ID 不吞可信事件',
    x.events.events.value.some((e) => e.id === 'lost-event'));
}

// 5. 内存 seen 有界
{
  const x = environment();
  for (let i = 0; i < 1000; i += 1) x.events.ingestFromStatus([event(`bulk-${i}`)]);
  record('1000 条不同事件后内存 seen ≤ 300',
    x.seen().size <= 300 && x.events.events.value.length <= 50,
    `seen=${x.seen().size} events=${x.events.events.value.length}`);
}

// 6. 去重不回退：同 ID 只一条，不同 ID 同文案保留
{
  const x = environment();
  x.events.ingestFromStatus([event('same-id')]);
  x.events.ingestFromStatus([event('same-id')]);
  x.events.ingestFromStatus([
    event('dup-a'), event('dup-b'),          // 同秒同文案、不同 ID
  ]);
  x.events.ingestFromStatus([event('dup-a')]); // 重复接收
  const ids = x.events.events.value.map((e) => e.id);
  record('同 ID 去重保留，不同 ID 同文案都保留',
    ids.filter((i) => i === 'same-id').length === 1
    && ids.includes('dup-a') && ids.includes('dup-b'),
    JSON.stringify(ids));
}

const failed = results.filter(([, ok]) => !ok).length;
console.log(`全部 ${results.length} 项断言，失败 ${failed}`);
process.exitCode = failed ? 1 : 0;
