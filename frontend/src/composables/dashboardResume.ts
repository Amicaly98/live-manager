import type { LiveStatus } from '@/types/api'

/** Dashboard-only view of a saved session. A stale read keeps display data but
 * never grants a new resume or task-start action. */
export interface DashboardResumeState {
  has_active: boolean
  resume_status_fresh: boolean
  resume_blocked_reason: string
  current_zone: string
  elapsed_seconds: number
  room_id: number
  duration_seconds: number
}

export function emptyDashboardResumeState(): DashboardResumeState {
  return {
    has_active: false,
    resume_status_fresh: false,
    resume_blocked_reason: '',
    current_zone: '',
    elapsed_seconds: 0,
    room_id: 0,
    duration_seconds: 0,
  }
}

function nonNegativeNumber(value: unknown): number {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : 0
}

/** Apply only a status snapshot with the backend's explicit resumability fact.
 * Missing or failed reads preserve the last displayed progress, marked stale. */
export function applyDashboardResumeStatus(
  previous: DashboardResumeState,
  status: LiveStatus | undefined,
): DashboardResumeState {
  if (!status || typeof status.resumable !== 'boolean') {
    return { ...previous, resume_status_fresh: false }
  }

  return {
    has_active: status.resumable,
    resume_status_fresh: true,
    resume_blocked_reason: String(status.resume_blocked_reason || ''),
    current_zone: String(status.current_zone || ''),
    elapsed_seconds: nonNegativeNumber(status.elapsed_seconds),
    room_id: nonNegativeNumber(status.room_id),
    duration_seconds: nonNegativeNumber(status.duration_seconds),
  }
}

export function canResumeDashboardTask(state: DashboardResumeState): boolean {
  return state.resume_status_fresh && state.has_active
}

export function canStartDashboardTask(state: DashboardResumeState): boolean {
  return state.resume_status_fresh && !state.has_active
}
