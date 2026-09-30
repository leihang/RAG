/**
 * 密级徽标 L1–L4（方案 05 §5.1「强制」表：色值逐字取自该表）。
 * 原则 P4「密级可见」：任何列表里的文档/知识库都必须带徽标。
 */
import { Tag, Tooltip } from 'antd'
import { LockOutlined, WarningOutlined } from '@ant-design/icons'
import { normalizeLevel, type Level } from '@/utils/permission'

const META: Record<Level, { name: string; color: string; warn?: boolean; lock?: boolean }> = {
  L1: { name: '公开', color: 'green' },
  L2: { name: '内部', color: 'blue' },
  L3: { name: '秘密', color: 'orange', warn: true },
  L4: { name: '机密', color: 'red', lock: true },
}

export interface ClearanceTagProps {
  level?: number | string | null
  /** 当前用户密级；给了才能判断"密级不足"并加锁 */
  clearance?: number | string | null
}

export default function ClearanceTag({ level, clearance }: ClearanceTagProps) {
  const lv = normalizeLevel(level)
  if (!lv) return <span>-</span>
  const meta = META[lv]
  const cl = normalizeLevel(clearance)
  const insufficient = cl != null && Number(lv.slice(1)) > Number(cl.slice(1))

  const tag = (
    <Tag
      color={meta.color}
      icon={meta.warn ? <WarningOutlined /> : meta.lock ? <LockOutlined /> : undefined}
      style={{ marginInlineEnd: 0, opacity: insufficient ? 0.55 : 1 }}
    >
      {lv} {meta.name}
      {insufficient ? ' 🔒' : ''}
    </Tag>
  )
  return insufficient ? <Tooltip title={`需 ${lv} 及以上密级（当前 ${cl}）`}>{tag}</Tooltip> : tag
}
