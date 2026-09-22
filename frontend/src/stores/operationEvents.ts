/**
 * operationEvents.ts - 近期操作日志的**统一事件store**（2026-09-21 工作包 A）。
 *
 * 旧数据流的问题（监督 O1/O2）：
 * - 服务器事件只在一个独立 30 秒定时器里摄取：首个可信状态响应明明已带
 *   `backend_events`，页面却要再等定时器写 localStorage、再等页面自己的
 *   轮询去读缓存，任务慢还会拖住日志呈现；
 * - localStorage 里"事件"与"已见 ID"分成两个 key、Dashboard 与 live store
 *   两个写者，缓存损坏被静默吞掉、已见 ID 又可能拦住记录补回；
 * - 去重按 time+message：同秒同文案的不同事件被吞。
 *
 * 现在的契约：
 * - **唯一摄取入口** `ingestFromStatus()`：只由 live store 的受控状态应用
 *   路径调用（响应已过代际/取消/版本门），30 秒定时器只是轮询驱动之一，
 *   不再有第二条写 localStorage 的路径；
 * - **按稳定身份去重**：服务器 `boot_id:seq`；同 ID 重复接收只保留一条，
 *   不同 ID 的同秒同文案都保留；
 * - **单一写者**：事件与已见 ID 在同一条缓存记录里，写失败只损失缓存、
 *   绝不阻塞可信新事件；损坏的缓存视为空，不抛错；
 * - **来源分阶段**：`source='server'` 是后台事实，`source='local'` 是用户
 *   操作意图（独立身份），界面分区展示，不用文案猜"同一件事"；
 * - **上下文隔离**：缓存按平台账号 uid 命名空间隔离；登出/切换账号时
 *   `handleContextSwitch()` 重新装载，旧账号的记录不会串进新会话；
 * - **有界**：最多 MAX_EVENTS 条、MAX_SEEN 个已见 ID；不建设无限审计库，
 *   服务端本来就只保留最近事件，重启前的历史不以"完整日志"的名义承诺。
 */
import { ref } from 'vue'

export interface OperationEvent {
  /** 稳定身份：服务器 = `boot_id:seq`；本地 = `local:{uuid}`；旧缓存 = legacy 前缀 */
  id: string
  tag: string
  type: string
  message: string
  time: string
  source: 'server' | 'local' | 'legacy'
  boot_id?: string
  seq?: number
}

interface CachePayload {
  events: OperationEvent[]
  seen: string[]
}

const MAX_EVENTS = 50
const MAX_SEEN = 300

/** 与服务器事件同一套兜底身份：旧缓存里没有 id/boot_id 的记录。 */
function legacyId(time: string, message: string): string {
  return `legacy:${time}:${message}`
}

/**
 * 当前账号上下文（按平台账号 uid 隔离，身份未确认时匿名共享）。
 *
 * **必须走真实缓存协议**：`user_info` 由 `useCache.set()` 写入，磁盘形状是
 * `{ data: {...}, expireAt }`（2026-09-21 R1 监督反例：直接读外层 uid 永远
 * 读不到，所有登录都退化成 `anon` 命名空间，跨账号隔离形同虚设）。
 *
 * 边界（与 useCache.get 的语义一致）：
 * - key 缺失（未登录/已登出）→ `anon`；
 * - `expireAt` 已过（缓存到期，身份未确认）→ `anon`——旧账号命名空间
 *   不被新会话误用，也不把缓存资料当成鉴权依据（这里只取命名空间标签）；
 * - JSON 损坏 → `anon`。
 */
function contextKey(): string {
  try {
    const raw = localStorage.getItem('user_info')
    if (!raw) return 'anon'
    const entry = JSON.parse(raw) as {
      data?: { uid?: unknown } | null
      uid?: unknown
      expireAt?: unknown
    }
    if (entry && typeof entry.expireAt === 'number' &&
        Date.now() > entry.expireAt) {
      return 'anon'                       // 缓存到期：身份未确认
    }
    // 协议形状是 {data:{uid},expireAt}；防御性兼容直接写 {uid} 的历史形态。
    const info = (entry && typeof entry === 'object' && 'data' in entry
      ? entry.data
      : entry) as { uid?: unknown } | null
    const uid = Number(info?.uid)
    if (Number.isFinite(uid) && uid > 0) return `u${uid}`
  } catch { /* 缓存损坏也按匿名处理 */ }
  return 'anon'
}

function cacheKey(): string {
  return `app_events_v2:${contextKey()}`
}

function nowLabel(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 兼容旧格式：只有 HH:MM:SS 的时间补当天日期。 */
function normalizeTime(t: string | undefined): string {
  const time = String(t || '')
  if (/^\d{2}:\d{2}:\d{2}$/.test(time)) {
    const d = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${time}`
  }
  return time
}

const events = ref<OperationEvent[]>([])
const seenIds = new Set<string>()
let loaded = false
/**
 * 当前已装载的命名空间。身份确认是**渐进**的：页面加载时 `user_info`
 * 可能尚未写入（认证读取未返回），此时摄取只能落匿名；等身份确认后，
 * 下一次摄取前必须切到真实账号命名空间——否则整场会话都停留在匿名
 * 缓存上（R1 浏览器矩阵实测）。
 */
let loadedContext: string | null = null

/**
 * 记录已见 ID，并让**内存集合本身**保持上界。
 *
 * 旧实现只约束序列化副本（persist 里的 slice），内存 Set 无限增长
 * （2026-09-21 R1 监督实测：1000 条不同事件后 seen=1000）。Set 保持
 * 插入序，裁剪最老的即可；被裁掉的是早已滚出展示窗口（MAX_EVENTS=50）
 * 的最老身份，不影响当前展示的去重。
 */
function rememberSeen(id: string): void {
  seenIds.add(id)
  while (seenIds.size > MAX_SEEN) {
    const oldest = seenIds.values().next().value
    if (oldest === undefined) break
    seenIds.delete(oldest)
  }
}

/** 用一份完整快照替换当前内存态，避免切换命名空间时残留旧事件/seen。 */
function replaceInMemory(nextEvents: OperationEvent[], nextSeen: string[] = []): void {
  events.value = nextEvents.slice(-MAX_EVENTS)
  seenIds.clear()
  for (const e of events.value) rememberSeen(e.id)
  for (const id of nextSeen) rememberSeen(String(id))
}

function loadFromCache(): void {
  const nextContext = contextKey()
  const nextCacheKey = `app_events_v2:${nextContext}`
  let migrated: OperationEvent[] | null = null
  // 旧格式迁移（一次性、尽力而为）：v1 的事件缓存仅供短暂兜底展示。
  try {
    if (!localStorage.getItem(nextCacheKey)) {
      const legacy = localStorage.getItem('app_events')
      if (legacy) {
        const parsed = JSON.parse(legacy) as Array<Record<string, unknown>>
        migrated = (Array.isArray(parsed) ? parsed : [])
          .slice(-MAX_EVENTS)
          .map((e) => {
            const time = normalizeTime(e.time as string)
            const id = (e.id as string) ||
              legacyId(time, String(e.message ?? ''))
            return {
              id,
              tag: String(e.tag ?? ''),
              type: String(e.type ?? 'info'),
              message: String(e.message ?? ''),
              time,
              source: 'legacy',
            }
          })
      }
    }
  } catch { /* 旧缓存损坏：按无历史处理，绝不阻塞新事件 */ }

  if (migrated !== null) {
    // 只迁入**事件自身**的身份：旧版"事件/已见 ID"两份缓存可能早已
    // 不一致（R1 监督反例：app_event_ids 里有 lost-event、app_events
    // 里没有），把孤立 ID 一并迁入会让新可信事件被永远吞掉、列表仍空。
    // 正常同 ID 重复抑制保留（迁移进来的事件照常去重）。
    replaceInMemory(migrated)
    loadedContext = nextContext
    loaded = true
    persist()
    // 迁移成功后旧 key 不再被任何写者触碰，清掉避免双份状态。
    try {
      localStorage.removeItem('app_events')
      localStorage.removeItem('app_event_ids')
    } catch { /* 清理失败无碍 */ }
    return
  }

  let nextEvents: OperationEvent[] = []
  let nextSeen: string[] = []
  try {
    const raw = localStorage.getItem(nextCacheKey)
    if (raw) {
      const payload = JSON.parse(raw) as CachePayload
      const list = Array.isArray(payload?.events) ? payload.events : []
      if (list.some((e) => !e || typeof e !== 'object' || typeof e.id !== 'string')) {
        throw new Error('invalid operation event cache')
      }
      nextEvents = list
      nextSeen = Array.isArray(payload?.seen) ? payload.seen : []
    }
  } catch {
    // 缓存损坏/结构错误：目标快照为空；新事件照常摄取。
  }
  // 缺失、空、损坏也必须整体替换，不能把上一命名空间留在内存里。
  replaceInMemory(nextEvents, nextSeen)
  loadedContext = nextContext
  loaded = true
}

/**
 * 摄取/本地写入前的命名空间自校正：身份与当前装载不一致（或尚未装载）
 * 就先重载。认证写入（登录/启动确认）不直接通知本 store，靠这里在
 * 下一次摄取时收敛——单一写者、无新增轮询。
 */
function ensureContextFresh(): void {
  const ctx = contextKey()
  if (!loaded || ctx !== loadedContext) {
    loadFromCache()
  }
}

function persist(): void {
  // 单一写者：事件与已见 ID 同一条记录，不会出现"ID 已见但记录缺失"。
  // 写失败（隐私模式/配额）只损失缓存，内存里的可信事件照常展示。
  try {
    const payload: CachePayload = {
      events: events.value.slice(-MAX_EVENTS),
      seen: Array.from(seenIds).slice(-MAX_SEEN),
    }
    localStorage.setItem(cacheKey(), JSON.stringify(payload))
  } catch { /* ignore */ }
}

function sortByTime(): void {
  events.value.sort((a, b) => String(a.time).localeCompare(String(b.time)))
  if (events.value.length > MAX_EVENTS) {
    events.value = events.value.slice(-MAX_EVENTS)
  }
}

export const operationEvents = {
  /** 响应式事件列表（页面直接订阅，不再经过 localStorage 中转）。 */
  events,

  /** 确保缓存已装载（挂载时调用；ingest/addLocal 内部也会兜底）。 */
  ensureLoaded(): void {
    if (!loaded) loadFromCache()
  },

  /**
   * 从一份**已通过门控**的状态响应摄取服务器事件。
   * 只由 live store 的 applyStatus（受控路径）调用；重复调用安全。
   */
  ingestFromStatus(backendEvents: unknown): void {
    ensureContextFresh()
    const list = Array.isArray(backendEvents) ? backendEvents : []
    if (list.length === 0) return
    let added = false
    for (const raw of list) {
      const e = raw as {
        tag?: string; type?: string; message?: string; time?: string;
        id?: string; seq?: number; boot_id?: string
      }
      const time = normalizeTime(e.time)
      const id = e.id || `${e.boot_id || 'legacy'}:${time}:${e.message}`
      if (seenIds.has(id)) continue
      rememberSeen(id)
      events.value.push({
        id,
        tag: String(e.tag ?? ''),
        type: String(e.type ?? 'info'),
        message: String(e.message ?? ''),
        time,
        source: 'server',
        boot_id: e.boot_id,
        seq: e.seq,
      })
      added = true
    }
    if (added) {
      sortByTime()
      persist()
    }
  },

  /** 本地操作意图：独立身份、独立来源，绝不与后台事实按文案合并。 */
  addLocal(tag: string, type: OperationEvent['type'], message: string): void {
    ensureContextFresh()
    const uuid = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    events.value.push({
      id: `local:${uuid}`,
      tag,
      type,
      message,
      time: nowLabel(),
      source: 'local',
    })
    sortByTime()
    persist()
  },

  /** 账号上下文切换（登录成功/登出）：重载对应命名空间的缓存。 */
  handleContextSwitch(): void {
    events.value = []
    seenIds.clear()
    loaded = false
    loadFromCache()
  },

  /** 测试/清理用：清空内存态（不动磁盘）。 */
  resetInMemory(): void {
    events.value = []
    seenIds.clear()
    loaded = false
  },
}
