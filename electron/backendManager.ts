/**
 * backendManager.ts - 后端进程生命周期状态机（E2/D2）与端口占用判定（E1/D2）
 *
 * 独立于 Electron 的纯逻辑模块：通过依赖注入实现，可在 Node 环境下
 * 直接单测（不 mock Electron）。
 *
 * E2/D2 语义：
 * - 单一 owner 状态机：idle → starting → ready → stopping → exited；
 *   starting/ready 之外拒绝重复 start；stopping 中拒绝 start；
 * - start 在每个 await（spawn/waitReady）之后复核代际与停止状态：
 *   停止期间晚到的子进程被立即回收，不会成为 ready 后端；
 *   停止期间晚到的健康结果不能把已停止的代际报告为 ready；
 * - close/error 回调携带 owner 身份（pid + generation）：任何旧回调
 *   不能清掉新代的引用、不能触发新代的自动重启；
 * - 退出期间（quitting）不自动复活后端；
 * - stop：先优雅（HTTP shutdown 有超时，可选是否请求平台停播）→
 *   再强杀进程树 → 等待确认退出；全程有界，返回是否确认回收。
 *
 * E1/D2 语义：
 * - parseNetstatListeners：解析 netstat -ano 输出，找出监听指定端口的 PID；
 * - PID 文件记录我们拉起后端时的完整命令行；回收前用可复核证据
 *   （实时查询进程命令行并比对）确认身份——PID 复用会使命令行不匹配，
 *   此时按未知占用处理（只提示，绝不 taskkill）。
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
  /** 请求优雅关闭（HTTP /api/shutdown）；stopPlatform=false 时不请求平台停播（D2） */
  requestShutdown(stopPlatform: boolean): Promise<boolean>;
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
  /** D2：停止请求标志——start() 在 spawn/waitReady 之后复核；
   *  spawn 尚未返回时的停止也能生效，迟到的子进程会被回收。 */
  private stopRequested = false;
  /** spawn 尚未返回时也必须有一个可等待的 owner 收尾边界。 */
  private pendingSpawn: {
    generation: number;
    settled: Promise<boolean>;
    resolve: (reclaimed: boolean) => void;
  } | null = null;
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

  private finishPendingSpawn(generation: number, reclaimed: boolean): void {
    const pending = this.pendingSpawn;
    if (!pending || pending.generation !== generation) return;
    this.pendingSpawn = null;
    pending.resolve(reclaimed);
  }

  /** Wait for a late spawn only within the caller's bounded stop budget. */
  private async waitPendingSpawn(
    pending: { settled: Promise<boolean> }, timeoutMs: number,
  ): Promise<boolean | undefined> {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const timeout = new Promise<undefined>(resolve => {
      timer = setTimeout(() => resolve(undefined), Math.max(0, timeoutMs));
    });
    const result = await Promise.race([pending.settled, timeout]);
    if (timer !== null) clearTimeout(timer);
    return result;
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
    this.stopRequested = false; // 新一代开始：清掉上一轮的停止请求
    this.generation += 1;
    const myGeneration = this.generation;
    this.state = 'starting';
    let resolvePendingSpawn: ((reclaimed: boolean) => void) | null = null;
    const pendingSettled = new Promise<boolean>(resolve => {
      resolvePendingSpawn = resolve;
    });
    this.pendingSpawn = {
      generation: myGeneration,
      settled: pendingSettled,
      // The resolver is assigned synchronously by the Promise constructor.
      resolve: (reclaimed: boolean) => resolvePendingSpawn?.(reclaimed),
    };
    let proc: OwnedProcess;
    try {
      proc = await this.deps.spawn();
    } catch (err) {
      this.finishPendingSpawn(myGeneration, true);
      this.state = 'exited';
      this.current = null;
      this.deps.log('error', `[backend] 启动失败: ${err}`);
      return { started: false, reason: 'spawn_failed' };
    }
    // D2：spawn 尚未返回期间发生了停止 → 迟到的子进程必须回收，
    // 不能成为存活后端，也不能报告 started=true。
    if (this.stopRequested || this.quitting || this.generation !== myGeneration) {
      this.deps.log('warn', `[backend] spawn 返回时代际已失效（停止/退出），回收迟到的子进程 PID=${proc.pid}`);
      let reclaimed = !proc.isAlive();
      if (proc.isAlive()) {
        try { await proc.killTree(); } catch { /* 尽力回收 */ }
        reclaimed = !proc.isAlive();
      }
      if (this.generation === myGeneration) {
        if (reclaimed) {
          if (this.current === proc) this.current = null;
          this.state = 'exited';
        } else {
          // stop() may have returned while spawn was pending.  The late child
          // is now the owned process and must remain retryable until exit.
          if (this.current === null) this.current = proc;
          if (this.current === proc) this.state = 'stopping';
        }
      }
      this.finishPendingSpawn(myGeneration, reclaimed);
      return { started: false, reason: 'stopped_during_spawn' };
    }
    this.current = proc;
    this.finishPendingSpawn(myGeneration, true);
    this.deps.log('info', `[backend] 已启动 (PID=${proc.pid}, 代际=${myGeneration})`);
    const ready = await this.deps.waitReady(30000);
    // D2：waitReady 期间停止/换代 → 迟到的健康结果不能报告 ready
    if (this.stopRequested || this.quitting || this.generation !== myGeneration || this.current !== proc) {
      this.deps.log('warn', `[backend] 等待就绪期间发生停止/换代（代际=${myGeneration}），不进入 ready`);
      let reclaimed = !proc.isAlive();
      if (proc.isAlive()) {
        try {
          const killIssued = await proc.killTree();
          reclaimed = !proc.isAlive();
          if (!killIssued && !reclaimed) {
            this.deps.log('warn', '[backend] 迟到启动的进程树仍存活，保留 owner 引用');
          }
        } catch { /* 尽力回收 */ }
      }
      if (this.current === proc) {
        if (reclaimed) {
          this.current = null;
          this.state = 'exited';
        } else {
          // A late health result must not erase a still-live owner after a
          // failed stop; keep the reference in stopping for a bounded retry.
          this.state = 'stopping';
        }
      }
      return { started: false, reason: 'stopped_during_start' };
    }
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
   * stopPlatform：传给 requestShutdown——"不停止并退出"必须为 false，
   * 避免退出路径触发平台下播（D2）。
   * 返回是否确认进程已退出（E2：等旧进程树确认回收后才允许拉起）。
   */
  async stop(graceful: boolean, killTimeoutMs: number = 10000,
             stopPlatform: boolean = true): Promise<boolean> {
    this.stopRequested = true;
    const proc = this.current;
    if (proc === null) {
      // spawn 可能仍在进行：不能把“尚未拿到句柄”当作已回收。等待迟到
      // 子进程在本次有界预算内完成；超时则保持 stopping，禁止新一代覆盖它。
      const pending = this.pendingSpawn;
      if (pending !== null) {
        this.state = 'stopping';
        const lateResult = await this.waitPendingSpawn(pending, killTimeoutMs);
        if (lateResult === undefined) {
          this.deps.log('warn', '[backend] spawn 尚未返回，停止收尾超时；保留待决 owner');
          return false;
        }
        if (!lateResult) {
          this.deps.log('warn', '[backend] 迟到 spawn 未确认回收；保留 owner 供重试');
          return false;
        }
        if (this.current === null) {
          this.state = 'exited';
          return true;
        }
        // 迟到 kill 已确认失败且 current 已被登记时，不能报告成功。
        return false;
      }
      if (this.state !== 'stopping') this.state = 'exited';
      return this.state === 'exited';
    }
    const generationAtEntry = this.generation;
    const previous = this.state;
    this.state = 'stopping';
    if (graceful) {
      try {
        const delivered = await this.deps.requestShutdown(stopPlatform);
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

  /** 重启后端（E2）：取消挂起重启 → 停止并等待回收 → 才拉起。
   *  D2：backend-restart 是显式用户动作，保留完整停止语义（含平台下播），
   *  与"不停止并退出"（stopPlatform=false）区分。 */
  async restart(killTimeoutMs: number = 10000): Promise<boolean> {
    this.cancelAutoRestart();
    const reclaimed = await this.stop(true, killTimeoutMs, true);
    if (!reclaimed) {
      this.deps.log('error', '[backend] 重启失败：旧进程未确认回收，不拉起新代');
      return false;
    }
    const r = await this.start();
    return r.started;
  }

  /**
   * 后端进程 close 回调（E2/D2）：
   * - 携带 owner 身份（pid + generation）时，只有与当前 owner 匹配的
   *   close 才清引用/触发自动重启；旧代回调对新一代无任何影响；
   * - quitting 期间不自动重启；
   * - 非零退出码且非退出期间 → 安排自动重启（可被 restart/quit 取消）。
   */
  handleClose(code: number | null, pid?: number | null, generation?: number): void {
    if (pid !== undefined && pid !== null && generation !== undefined) {
      const isCurrentOwner = this.current !== null
        && this.current.pid === pid
        && this.generation === generation;
      if (!isCurrentOwner) {
        this.deps.log('info',
          `[backend] 忽略旧代 close 回调 (PID=${pid}, 代际=${generation}；` +
          `当前=${this.current ? this.current.pid : 'none'}, 代际=${this.generation})`);
        return;
      }
    } else if (this.current === null) {
      // 无身份且无当前 owner：无事可做
      this.deps.log('info', '[backend] 收到无身份 close 且当前无 owner，忽略');
      return;
    }
    this.current = null;
    this.state = 'exited';
    this.deps.log('info', `[backend] 后端退出 code=${code} (代际=${this.generation})`);
    if (this.quitting) return;
    if (code !== 0) {
      this.deps.log('warn', '[backend] 后端异常退出，安排自动重启…');
      this.restartTimer = setTimeout(() => {
        this.restartTimer = null;
        void this.start();
      }, this.deps.autoRestartDelayMs ?? 5000);
    }
  }

  /** 后端进程 error 回调（spawn 失败等）；D2：携带身份核对，旧代不记入新代 */
  handleError(err: Error, pid?: number | null, generation?: number): void {
    if (pid !== undefined && pid !== null && generation !== undefined
        && (this.current === null || this.current.pid !== pid
            || this.generation !== generation)) {
      this.deps.log('info',
        `[backend] 忽略旧代 error 回调 (PID=${pid}, 代际=${generation})`);
      return;
    }
    this.deps.log('error', `[backend] 进程错误: ${err.message}`);
  }
}

// ==================== E1/D2：端口占用判定 ====================

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

/** PID 文件记录：PID + 拉起时的完整命令行（身份证据） */
export interface OwnedPidRecord { pid: number; command: string }

export type PortConflictKind = 'own_stale' | 'foreign' | 'none';

/**
 * 端口冲突决策（E1/D2：绝不杀无法确认身份的进程）。
 *
 * - 'own_stale'：所有监听者都在 PID 文件记录中，**且**身份验证通过
 *   （实时查询的进程命令行与登记命令行一致）。D2：仅有相同 PID 数字
 *   不足以认领——Windows PID 复用会把无关进程认成自家旧后端；
 * - 'foreign'：存在未知监听者，或身份无法验证 → 报错提示用户处理，
 *   不自动 taskkill；
 * - 'none'：端口空闲。
 *
 * identityMatches(record) 由调用方注入：生产实现查询系统进程命令行；
 * 测试注入替身。查询失败/无法验证一律按未验证处理（返回 false）。
 */
export function classifyPortConflict(
  listenerPids: number[],
  ownedRecords: OwnedPidRecord[],
  identityMatches: (record: OwnedPidRecord) => boolean
): PortConflictKind {
  if (listenerPids.length === 0) return 'none';
  const byPid = new Map(ownedRecords.map(r => [r.pid, r]));
  for (const pid of listenerPids) {
    const record = byPid.get(pid);
    if (!record) return 'foreign';
    let verified = false;
    try {
      verified = !!identityMatches(record);
    } catch { verified = false; }
    if (!verified) return 'foreign';
  }
  return 'own_stale';
}
