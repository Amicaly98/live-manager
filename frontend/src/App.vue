<template>
  <div class="app-root">
    <!--
      启动阶段：路由还没进入时也不能只剩底色。
      这里不依赖任何页面组件加载成功——它自己就在根层。
    -->
    <div v-if="phase !== 'ready'" class="boot-screen" role="status" aria-live="polite">
      <div class="boot-card">
        <div class="boot-title">直播控制系统</div>

        <template v-if="phase === 'connecting'">
          <div class="boot-spinner" aria-hidden="true"></div>
          <div class="boot-line">{{ BOOT_MESSAGES.connecting }}</div>
          <div class="boot-hint">首次进入需要读取登录状态，请稍候</div>
        </template>

        <template v-else>
          <div class="boot-line boot-line-failed">{{ BOOT_MESSAGES.failed }}</div>
          <div class="boot-hint">{{ failureHint }}</div>
          <div class="boot-actions">
            <el-button type="primary" :loading="retrying" @click="onRetry">
              重试
            </el-button>
            <el-button @click="onReload">重新加载</el-button>
          </div>
        </template>
      </div>
    </div>

    <template v-else>
      <!--
        退出失败不在后台静默：主进程保持应用存活（不销毁后端、不提前释放账号
        占用），这里把"失败/待核验"如实摆在页面上，并提供重试。
      -->
      <div v-if="quitFailure" class="quit-failure-bar" role="alert">
        <span class="quit-failure-text">退出未完成：{{ quitFailure }}</span>
        <span class="quit-failure-actions">
          <el-button size="small" type="primary" plain @click="retryQuit">
            重试退出
          </el-button>
          <el-button size="small" @click="quitFailure = ''">留在应用</el-button>
          <!-- 与"已确认安全退出"明确区分：只在优雅退出失败之后才出现，且按钮
               自己就写明"不等待后端确认"，不会被当成一次成功的收尾。 -->
          <el-button size="small" type="danger" plain @click="forceQuit">
            仍要退出（不等待后端确认）
          </el-button>
        </span>
      </div>
      <router-view />
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * App.vue（桌面移植阶段 2）。
 *
 * 与服务端版的差别：
 *  - 桌面没有面板密码，业务加载的开关只有"启动结论成立 + 平台已登录"两个条件；
 *  - **保留桌面托盘退出**：三种退出语义在这里明确分开（见 `requestQuit`）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { boot, BOOT_MESSAGES, BOOT_FAILURE_HINTS } from '@/boot'
import { useTaskStore } from '@/stores/tasks'
import { useLiveStore } from '@/stores/live'

const router = useRouter()
const taskStore = useTaskStore()
const liveStore = useLiveStore()

const phase = ref(boot.phase)
const failure = ref(boot.failure)
const retrying = ref(false)

const failureHint = computed(() => {
  const kind = failure.value?.kind
  return kind ? BOOT_FAILURE_HINTS[kind] : ''
})

/**
 * 业务加载（事件轮询 + 任务列表）的**唯一开关**。
 *
 * 由启动状态驱动：只有"当前这一代"确实成立、且平台已登录时才启动；状态一旦被
 * 作废（登出 / 401 / 重新登录）就停掉受保护读取。
 */
let businessActive = false

function syncBusinessLoads() {
  const result = boot.result
  const shouldRun = boot.phase === 'ready' && boot.isCurrent(result)
                    && !!result && result.ok && result.loggedIn
  if (shouldRun && !businessActive) {
    businessActive = true
    liveStore.startEventPolling()          // 本身幂等：重复调用不会叠加计时器
    void liveStore.refreshStatus()
    void taskStore.fetchTasks()
  } else if (!shouldRun && businessActive) {
    businessActive = false
    liveStore.teardown()                   // 停轮询 + 中止在途状态读 + 停本地绘制
    taskStore.stopTaskReads()              // 取消在途/退避中的任务读；旧代响应也不得写回
  }
}

/** 恢复成功后，回到用户本来想去的地址（导航可能曾被启动读取挡下）。 */
async function resumeNavigation() {
  const target = boot.pendingPath
  if (!target) return
  boot.pendingPath = null
  const current = router.currentRoute.value
  if (!current.matched.length || current.fullPath !== target) {
    try {
      await router.replace(target)
    } catch {
      /* 导航被后续守卫拒绝属于正常，不在此处理 */
    }
  }
}

/**
 * 把启动读取推进到"当前这一代"的结论。
 *
 * 状态被作废（`boot.invalidate`）会清空结果；这里负责**重新驱动一次读取**，
 * 否则界面会停在"正在连接…"而没人发起新的读取。同一代只驱动一次
 * （`drivenGeneration`），不会叠加请求。
 */
let drivenGeneration = -1

async function driveBoot(): Promise<void> {
  const snapshot = boot.snapshot
  // 需要驱动的情形：还没有结论（首次启动、或认证事实刚被作废）。
  // 'failed' 不自动重试——那是用户点"重试"才该发生的事。
  if (boot.result !== null) return
  if (snapshot.phase === 'failed') return
  if (drivenGeneration === snapshot.generation) return
  drivenGeneration = snapshot.generation
  // 先让出一次微任务：不在 `_emit()` 的同步调用栈里继续发起读取，避免重入。
  await Promise.resolve()
  if (boot.snapshot.generation !== snapshot.generation) return
  const result = await boot.ensure()
  if (!boot.isCurrent(result)) return          // 期间又被作废：等下一次驱动
  if (boot.phase === 'ready') await resumeNavigation()
  syncBusinessLoads()
}

// ==================== 托盘退出（桌面） ====================
//
// 三种退出语义必须分开，任何一种都不许伪装成另一种：
//  1. **停止并退出**（stop_live=true）：请后端经同一 ControlService 停播并核对
//     关房，再回收本地资源；
//  2. **不停止并退出**（stop_live=false）：**不**发送平台 Stop，只回收自有本地
//     媒体与后端并保留恢复依据（外部 OBS / 外来 FFmpeg / 身份不明的端口占用者
//     一个都不碰）；
//  3. **仍要退出（不等待后端确认）**：只有在 1/2 都失败之后才由用户**显式**选择，
//     并且绝不对外宣称"安全退出"。
//
// 后端未确认平台停止或本地资源未收齐时，页面**保留**并显示失败/待核验，允许
// 重试——绝不因等待超时静默强杀再报成功。

const closeGuard = ref(false)
/** 上一轮优雅退出失败的原因（来自主进程）；空串表示没有待核验的退出。 */
const quitFailure = ref('')
/** 上一次尝试的退出方式：重试必须复用它，不能把"停止并退出"悄悄降级。 */
let lastQuitStopLive = false

async function requestQuit(stopLive: boolean): Promise<void> {
  lastQuitStopLive = stopLive
  try {
    const result = await window.electronAPI?.confirmQuit(stopLive)
    if (result && result.success) {
      // 主进程已确认收尾，应用即将退出：释放页面侧守卫。
      quitFailure.value = ''
      closeGuard.value = false
      return
    }
    // 后端仍在工作或本地资源未收齐：主进程保持应用存活。这里**不**静默强杀，
    // 只把失败/待核验如实摆出来，并让下一次重试仍然可行。
    quitFailure.value = (result && result.reason)
      || '后端尚未确认收尾，应用保持运行（未销毁后端、未释放账号占用）'
    closeGuard.value = false
  } catch {
    // IPC 拒绝同样是一次失败的退出尝试：保持应用可用，允许后续重试。
    quitFailure.value = '退出请求未能送达主进程，应用保持运行'
    closeGuard.value = false
  }
}

/** 重试上一次的退出方式（同一个 stop_live 语义，不是降级成"不停止"）。 */
function retryQuit(): void {
  if (closeGuard.value) return
  closeGuard.value = true
  void requestQuit(lastQuitStopLive)
}

/**
 * **显式**放弃等待后端确认并结束本应用。
 *
 * 与"已确认安全退出"是两件事，界面与日志都不把它说成后者：它只在优雅退出
 * 失败之后由用户再点一次才发生，按钮文案自己就写明代价。
 */
async function forceQuit(): Promise<void> {
  if (closeGuard.value) return
  closeGuard.value = true
  try {
    const result = await window.electronAPI?.forceQuit()
    if (result && result.confirmed) {
      quitFailure.value = ''
      closeGuard.value = false
      return
    }
    quitFailure.value = '放弃等待的请求也未落实，应用保持运行'
  } catch {
    quitFailure.value = '强制退出请求未能送达主进程，应用保持运行'
  }
  closeGuard.value = false
}

async function handleTrayQuit() {
  if (closeGuard.value) return
  closeGuard.value = true
  quitFailure.value = ''
  // 新协议：是否"正在直播中"只按投影事实判断——目标是 running（含等待任务）、
  // 平台已确认在播、或正在停止收尾，都属于"这场还没结束"。
  if (liveStore.live || liveStore.running || liveStore.closing) {
    ElMessageBox.confirm(
      '正在直播中，关闭应用将同时停止直播。\n\n是否停止直播并退出？',
      '确认退出',
      {
        confirmButtonText: '停止并退出',
        cancelButtonText: '不停止并退出',
        distinguishCancelAndClose: true,
        type: 'warning',
      },
    ).then(() => requestQuit(true)).catch((action: string) => {
      closeGuard.value = false
      if (action === 'cancel') void requestQuit(false)
    })
    return
  }
  ElMessageBox.confirm(
    '确定要退出应用吗？',
    '确认退出',
    { confirmButtonText: '退出', cancelButtonText: '取消', type: 'info' },
  ).then(() => requestQuit(false)).catch(() => { closeGuard.value = false })
}

const unsubscribe = boot.subscribe((snapshot) => {
  phase.value = snapshot.phase
  failure.value = snapshot.failure
  if (snapshot.phase !== 'connecting') retrying.value = false
  void driveBoot()
  syncBusinessLoads()
})

onMounted(async () => {
  if (window.electronAPI) window.electronAPI.onTrayQuit(handleTrayQuit)
  await driveBoot()
  syncBusinessLoads()
})

async function onRetry() {
  retrying.value = true
  try {
    await boot.retry()
  } finally {
    retrying.value = false
  }
  drivenGeneration = boot.generation
  if (boot.phase === 'ready') await resumeNavigation()
  syncBusinessLoads()
}

/** 重新加载：用于资源/初始化失败。不做自动无限刷新，只有用户点才发生。 */
function onReload() {
  try {
    globalThis.location?.reload()
  } catch {
    /* ignore */
  }
}

onUnmounted(() => {
  unsubscribe()
  businessActive = false
  liveStore.teardown()
  taskStore.stopTaskReads()
})
</script>

<style scoped>
.app-root {
  min-height: 100vh;
}

.boot-screen {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  box-sizing: border-box;
  background: #f5f7fa;
}

.boot-card {
  max-width: 420px;
  width: 100%;
  padding: 32px 28px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
  text-align: center;
}

.boot-title {
  font-size: 20px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 20px;
}

.boot-spinner {
  width: 28px;
  height: 28px;
  margin: 0 auto 14px;
  border: 3px solid #e4e7ed;
  border-top-color: #00a1d6;
  border-radius: 50%;
  animation: boot-spin 0.9s linear infinite;
}

@keyframes boot-spin {
  to {
    transform: rotate(360deg);
  }
}

.boot-line {
  font-size: 15px;
  color: #303133;
}

.boot-line-failed {
  color: #e6a23c;
}

.boot-hint {
  margin-top: 8px;
  font-size: 13px;
  color: #909399;
  line-height: 1.6;
}

.boot-actions {
  margin-top: 18px;
  display: flex;
  gap: 12px;
  justify-content: center;
}

.quit-failure-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 16px;
  background: #fef0f0;
  border-bottom: 1px solid #fde2e2;
  color: #f56c6c;
  font-size: 13px;
}

.quit-failure-text {
  flex: 1;
  min-width: 220px;
}

.quit-failure-actions {
  display: inline-flex;
  gap: 8px;
}
</style>
