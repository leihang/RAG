import { describe, expect, it } from 'vitest'
import { canAccessLevel, has, hasAll, hasAny, normalizeLevel } from './permission'

describe('权限纯函数（方案 05 §6.4）', () => {
  it('has / hasAll / hasAny 不把角色名当权限点', () => {
    const perms = ['doc:document:read', 'doc:document:create']
    expect(has(perms, 'doc:document:read')).toBe(true)
    expect(has(perms, 'super_admin')).toBe(false)
    expect(hasAll(perms, ['doc:document:read', 'doc:document:create'])).toBe(true)
    expect(hasAll(perms, ['doc:document:read', 'perm:user:read'])).toBe(false)
    expect(hasAny(perms, ['perm:user:read', 'doc:document:read'])).toBe(true)
  })

  it('密级归一化同时接受 L2 与 2 两种写法', () => {
    expect(normalizeLevel(2)).toBe('L2')
    expect(normalizeLevel('l3')).toBe('L3')
    expect(normalizeLevel(9)).toBeNull()
    expect(normalizeLevel(null)).toBeNull()
  })

  it('密级判定：文档 level ≤ 用户 clearance', () => {
    expect(canAccessLevel(2, 1)).toBe(true)
    expect(canAccessLevel(2, 3)).toBe(false)
    expect(canAccessLevel('L4', 4)).toBe(true)
  })

  it('拿不到密级时不拦显示（交给后端与数据层）', () => {
    expect(canAccessLevel(null, 4)).toBe(true)
    expect(canAccessLevel(1, undefined)).toBe(true)
  })
})
