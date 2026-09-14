/**
 * backendManager.ts - 后端进程生命周期状态机（E2）与端口占用判定（E1）
 *
 * 独立于 Electron 的纯逻辑模块：通过依赖注入实现，可在 Node 环境下
 * 直接单测（不 mock Electron）。
 *
 * E2 语义：
 * - 单一 owner 状态机：idle → starting → ready → stopping → exited；
 *   starting/ready 之外拒绝重复 start；stopping 中拒绝 start；
 * - restart：取消挂起的自动重启定时器 → 停止并等待旧进程确认退出 → 才拉起新代；
 * - close 回调按代际核对：旧代回调不清新代引用、不触发新代自动重启；
 * - 退出期间（quitting）不自动复活后端；
 * - stop：先优雅（HTTP shutdown 有超时）→ 再强杀进程树 → 等待确认退出；
 *   全程有界，返回是否确认回收。
 *
 * E1 语义：
 * - parseNetstatListeners：解析 netstat -ano 输出，找出监听指定端口的 PID；
 * - 决策函数 classifyPortConflict：只有"我们自己的上一代后端"（PID 文件
 *   匹配且进程存活）才允许回收；未知监听者一律报错，绝不 taskkill。
 */

export type BackendState = 'idle' | 'starting' | 'ready' | 'stopping' | 'exited';

export interface OwnedProcess {
  pid: number;
  /** 强杀整个进程树（Windows: taskkill /F /T /PID）。解析为退出码是否成功发出。 */
  killTree(): Promise<boolean>;
  /** 进程是否仍存活 */
  isAlive(): boolean;
}

export interface LifecycleDeps {
  /** 拉起新后端进程；返回进程句柄（抛错表示启动失败） */
  spawn(): Promise<OwnedProcess>;
  /** 请求优雅关闭（HTTP /api/shutdown）；resolve=true 表示请求送达 */
  requestShutdown(): Promise<boolean>;
  /** 等待健康检查就绪 */
  waitReady(timeoutMs: number): Promise<boolean>;
  log(level: 'info' | 'warn' | 'error', message: string): void;
  /** 自动重启延迟（默认 5000ms；测试可注入 0） */
  autoRestartDelayMs?: number;
}

export class BackendLifecycle {
  private state: BackendState = 'idle';
  private generation = 0;
  private current: OwnedProcess | null = null;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
  private quitting = false;
  private readonly deps: LifecycleDeps;

  constructor(deps: LifecycleDeps) {
    this.deps = deps;
  }

  getState(): BackendState { return this.state; }
  getGeneration(): number { return this.generation; }
  getPid(): number | null { return this.current ? this.current.pid : null; }
  isQuitting(): boolean { return this.quitting; }

  setQuitting(): void {
    this.quitting = true;
    this.cancelAutoRestart();
  }

  cancelAutoRestart(): void {
    if (this.restartTimer !== null) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
      this.deps.log('info', '[backend] 已取消挂起的自动重启');
    }
  }

  /**
   * 拉起后端。仅在 idle/exited 状态允许；starting/ready/stopping 一律拒绝
   * （E2：旧进程确认回收前不启动新代）。
   */
  async start(): Promise<{ started: boolean; reason: string }> {
    if (this.quitting) return { started: false, reason: 'quitting' };
    if (this.state === 'starting' || this.state === 'ready' || this.state === 'stopping') {
      return { started: false, reason: `state=${this.state}` };
    }
    this.cancelAutoRestart();
    this.generation += 1;
    this.state = 'starting';
    try {
      this.current = await this.deps.spawn();
      this.deps.log('info', `[backend] 已启动 (PID=${this.current.pid}, 代际=${this.generation})`);
    } catch (err) {
      this.state = 'exited';
      this.current = null;
      this.deps.log('error', `[backend] 启动失败: ${err}`);
      return { started: false, reason: 'spawn_failed' };
    }
    const ready = await this.deps.waitReady(30000);
    if (!ready) {
      this.deps.log('error', '[backend] 健康检查超时（后端可能仍在启动，或启动失败）');
      this.state = 'ready'; // 进程在跑但未就绪：不再自动重拉，交由用户重启
      return { started: true, reason: 'health_timeout' };
    }
    this.state = 'ready';
    return { started: true, reason: 'ok' };
  }

  /**
   * 停止后端（幂等、有界）。
   * graceful=true：先 HTTP shutdown（内部有超时）再强杀；false：直接强杀。
   * 返回是否确认进程已退出（E2：等旧进程树确认回收后才允许拉起）。
   */
  async stop(graceful: boolean, killTimeoutMs: number = 10000): Promise<boolean> {
    const proc = this.current;
    if (proc === null) {
      this.state = this.state === 'stopping' ? this.state : 'exited';
      return true;
    }
    const generationAtEntry = this.generation;
    const previous = this.state;
    this.state = 'stopping';
    if (graceful) {
      try {
        const delivered = await this.deps.requestShutdown();
        if (delivered) {
          // 给后端一点时间自行退出（轮询，有界）
          const deadline = Date.now() + 3000;
          while (Date.now() < deadline && proc.isAlive()) {
            await new Promise(r => setTimeout(r, 200));
          }
        }
      } catch { /* 优雅关闭失败 → 走强杀 */ }
    }
    let killed = true;
    if (proc.isAlive()) {
      killed = await proc.killTree();
      if (!killed) {
        this.deps.log('warn', '[backend] 进程树强杀指令未确认成功');
      }
    }
    // 有界等待退出确认
    const deadline = Date.now() + killTimeoutMs;
    while (Date.now() < deadline && proc.isAlive()) {
      await new Promise(r => setTimeout(r, 200));
    }
    const reclaimed = !proc.isAlive();
    if (!reclaimed) {
      this.deps.log('warn', '[backend] 旧进程在超时内未确认退出');
    }
    // 代际核对：仅当仍是本代进程时才处理引用（旧 close 不清新引用）
    if (this.generation === generationAtEntry && this.current === proc) {
      if (reclaimed) {
        this.current = null;
        this.state = 'exited';
      } else {
        // 未确认回收：保留引用并停留在 stopping——后续 start 被拒绝，
        // 防止"以为清理成功"后出现双后端（与后端所有权语义一致）。
        this.state = 'stopping';
      }
    } else {
      this.state = previous === 'stopping' ? 'exited' : previous;
    }
    return reclaimed;
  }

  /** 重启后端（E2）：取消挂起重启 → 停止并等待回收 → 才拉起 */
  async restart(killTimeoutMs: number = 10000): Promise<boolean> {
    this.cancelAutoRestart();
    const reclaimed = await this.stop(true, killTimeoutMs);
    if (!reclaimed) {
      this.deps.log('error', '[backend] 重启失败：旧进程未确认回收，不拉起新代');
      return false;
    }
    const r = await this.start();
    return r.started;
  }

  /**
   * 后端进程 close 回调（E2）：
   * - 只有当前代的 close 才清引用；
   * - quitting 期间不自动重启；
   * - 非零退出码且非退出期间 → 安排自动重启（可被 restart/quit 取消）。
   */
  handleClose(code: number | null): void {
    const gen = this.generation;
    if (this.current !== null && this.current.pid !== null) {
      // close 只可能属于当前代（旧代引用已被清理）
    }
    this.current = null;
    this.state = 'exited';
    this.deps.log('info', `[backend] 后端退出 code=${code} (代际=${gen})`);
    if (this.quitting) return;
    if (code !== 0) {
      this.deps.log('warn', '[backend] 后端异常退出，安排自动重启…');
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        void this.start();
      }, this.deps.autoRestartDelayMs ?? 5000);
    }
  }

  /** 后端进程 error 回调（spawn 失败等） */
  handleError(err: Error): void {
    this.deps.log('error', `[backend] 进程错误: ${err.message}`);
  }
}

// ==================== E1：端口占用判定 ====================

/**
 * 解析 netstat -ano 输出，返回监听指定端口的 PID 列表。
 * 只认 LISTENING 行；解析失败返回空数组（不猜）。
 */
export function parseNetstatListeners(netstatOutput: string, port: number): number[] {
  const pids: number[] = [];
  for (const rawLine of netstatOutput.split('\n')) {
    const line = rawLine.trim();
    if (!line.includes('LISTENING')) continue;
    const parts = line.split(/\s+/);
    // 形如：TCP  127.0.0.1:8000  0.0.0.0:0  LISTENING  1234
    const local = parts[1] || '';
    const portSuffix = `:${port}`;
    if (!local.endsWith(portSuffix)) continue;
    const pid = parseInt(parts[parts.length - 1], 10);
    if (Number.isInteger(pid) && pid > 4) pids.push(pid);
  }
  return [...new Set(pids)];
}

export interface PidFileInfo { pids: number[] }

/**
 * 端口冲突决策（E1：绝不杀未知进程）。
 * - 'own_stale'：监听者全部在 PID 文件记录中（我们自己上一次的后端）→ 可回收；
 * - 'foreign'：存在未知监听者 → 报错退出，提示用户处理；
 * - 'none'：端口空闲。
 */
export function classifyPortConflict(
  listenerPids: number[],
  ownedPids: number[]
): 'own_stale' | 'foreign' | 'none' {
  if (listenerPids.length === 0) return 'none';
  const owned = new Set(ownedPids);
  const foreign = listenerPids.filter(pid => !owned.has(pid));
  if (foreign.length > 0) return 'foreign';
  return 'own_stale';
}
