/** 纯 UI 状态：侧栏折叠、紧凑模式。 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AppState {
  collapsed: boolean
  compact: boolean
  toggleCollapsed: () => void
  setCompact: (v: boolean) => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      collapsed: false,
      compact: false,
      toggleCollapsed: () => set((s) => ({ collapsed: !s.collapsed })),
      setCompact: (v) => set({ compact: v }),
    }),
    { name: 'ekbs.app' },
  ),
)
