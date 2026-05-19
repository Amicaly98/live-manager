/**
 * useCache — 带 TTL 的本地缓存 composable
 *
 * 封装 localStorage，支持：
 * - 自动 JSON 序列化/反序列化
 * - TTL 过期时间（默认 30 分钟）
 * - 过期自动返回 null
 * - 类型安全（泛型）
 */

const DEFAULT_TTL_MS = 30 * 60 * 1000 // 30 分钟

interface CacheEntry<T> {
  data: T
  expireAt: number // timestamp
}

export function useCache() {
  /** 写入缓存 */
  function set<T>(key: string, data: T, ttlMs: number = DEFAULT_TTL_MS): void {
    const entry: CacheEntry<T> = {
      data,
      expireAt: Date.now() + ttlMs,
    }
    try {
      localStorage.setItem(key, JSON.stringify(entry))
    } catch {
      // localStorage 满或不可用，静默失败
    }
  }

  /** 读取缓存（过期返回 null） */
  function get<T>(key: string): T | null {
    try {
      const raw = localStorage.getItem(key)
      if (!raw) return null

      const entry = JSON.parse(raw) as CacheEntry<T>
      if (Date.now() > entry.expireAt) {
        localStorage.removeItem(key)
        return null
      }
      return entry.data
    } catch {
      return null
    }
  }

  /** 删除指定缓存 */
  function remove(key: string): void {
    localStorage.removeItem(key)
  }

  /** 清除所有过期缓存 */
  function prune(): void {
    const now = Date.now()
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const key = localStorage.key(i)
      if (!key) continue
      try {
        const raw = localStorage.getItem(key)
        if (!raw) continue
        const entry = JSON.parse(raw)
        if (entry?.expireAt && now > entry.expireAt) {
          localStorage.removeItem(key)
        }
      } catch {
        // 非缓存数据，跳过
      }
    }
  }

  return { get, set, remove, prune }
}

/** 单例导出（全局共享同一 prune 策略） */
const cache = useCache()
export default cache
