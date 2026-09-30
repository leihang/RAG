/**
 * 权限判断**纯函数**（方案 05 §6.4：可单测，不依赖 React）。
 *
 * 前端只做"显示与否"的体验优化；**后端对每个权限点独立校验**（方案原则 P3）。
 */
export type Level = 'L1' | 'L2' | 'L3' | 'L4'

export const LEVEL_ORDER: Record<Level, number> = { L1: 1, L2: 2, L3: 3, L4: 4 }

export function normalizeLevel(level: number | string | null | undefined): Level | null {
  if (level == null) return null
  const s = String(level).toUpperCase()
  if (s === 'L1' || s === 'L2' || s === 'L3' || s === 'L4') return s
  const n = Number(level)
  if (n >= 1 && n <= 4) return `L${n}` as Level
  return null
}

export function has(permissions: readonly string[], code: string): boolean {
  // super_admin 由后端在 permissions[] 里展开，这里不做角色通配（避免前端造特权）
  return permissions.includes(code)
}

export function hasAll(permissions: readonly string[], codes: readonly string[]): boolean {
  return codes.every((c) => has(permissions, c))
}

export function hasAny(permissions: readonly string[], codes: readonly string[]): boolean {
  return codes.some((c) => has(permissions, c))
}

/** 密级是否够（文档 level ≤ 用户 clearance）。 */
export function canAccessLevel(
  clearance: number | string | null | undefined,
  level: number | string | null | undefined,
): boolean {
  const c = normalizeLevel(clearance)
  const l = normalizeLevel(level)
  if (!c || !l) return true // 拿不到密级时不拦显示，由后端与数据层负责
  return LEVEL_ORDER[l] <= LEVEL_ORDER[c]
}
