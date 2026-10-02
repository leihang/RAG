/**
 * 目录导入页的**纯函数**（报文整形 + 能力判定），与 `pages/parse/parseModel.ts`、
 * `pages/docs/docUtils.ts` 同一路数：页面只消费判定结果，判定本身能单独被 vitest 覆盖。
 *
 * 字段名全部按实测报文写（`ekbs/api/imports.py` + `ekbs/ingest/dir_import.py`）：
 *
 * * `POST /import/scan` 的 `preview[]` 每行是 `{rel_path, size_bytes, action, reason, doc_id, importable}`，
 *   其中 `action ∈ {new, duplicate, too_large, unsupported}`；
 * * 预览最多回 200 条（`preview_limit`），而 `importable` 是**全量**计数 —— 两者不等就说明被截断了。
 */
import type { ImportPreviewItem, ImportScanResp } from '@/api/types'

/** 扫描预览一行的动作 → 界面标签（颜色沿用项目里的 Tag 语义色）。 */
const ACTION_META: Record<string, { text: string; color: string }> = {
  new: { text: '可导入', color: 'green' },
  duplicate: { text: '重复（已在库）', color: 'default' },
  too_large: { text: '超过大小限制', color: 'orange' },
  unsupported: { text: '格式不支持', color: 'red' },
}

/** 未知动作不编造文案，原样显示（后端加了新动作时界面不会骗人）。 */
export function actionMeta(action: string | undefined): { text: string; color: string } {
  const key = String(action ?? '')
  return ACTION_META[key] ?? { text: key || '未知', color: 'default' }
}

/**
 * 行是否可导入：**以后端的 `importable` 为准**，缺失时才退回 `action === 'new'`。
 * 不在这里重算规则（重算就是"两个地方各写一份判据"）。
 */
export function isImportable(row: ImportPreviewItem): boolean {
  if (typeof row.importable === 'boolean') return row.importable
  return row.action === 'new'
}

/** 默认勾选：只勾「可导入且 action === 'new'」的行 —— 重复/超限/不支持默认不选，免得误报。 */
export function defaultSelectedPaths(preview: readonly ImportPreviewItem[] | undefined): string[] {
  return (preview ?? []).filter((r) => isImportable(r) && r.action === 'new').map((r) => String(r.rel_path))
}

/**
 * 预览是否被截断。
 *
 * 判定用**计数**而不是只信 `preview_truncated` 标志：`importable > preview.length` 本身就
 * 说明还有没列出来的可导入文件。截断时若拿勾选去导入，后端只会登记列出来的那 200 份，
 * 界面却显示"已完成" —— 这是本页最需要挡住的一类静默错误。
 */
export function previewTruncated(scan: ImportScanResp | null | undefined): boolean {
  if (!scan) return false
  if (scan.preview_truncated === true) return true
  return Number(scan.importable ?? 0) > (scan.preview?.length ?? 0)
}

/**
 * 阻断项（合规闸 / L3-L4 入库闸 / 密级-部门不匹配）。
 * 有值就必须禁止导入：后端 `enqueue_files` 会整批 409，前端先拦能少一次白跑。
 */
export function importBlockers(scan: ImportScanResp | null | undefined): string[] {
  return scan?.blockers ?? []
}

/** 后端错误 → 界面可读文案。403/404/422/409 这几种在导入链路上各有确切含义，不能都压成"请求失败"。 */
export function describeImportError(err: unknown): string {
  const status = (err as { status?: number } | null)?.status
  const text = err instanceof Error ? err.message : String(err)
  switch (status) {
    case 403:
      return `无权限，或路径超出 raw 目录（403）：${text}`
    case 404:
      return `目录不存在（404）：${text}`
    case 409:
      return `导入被拒绝（409）：${text}`
    case 422:
      return `请求参数不符合后端要求（422）：${text}`
    default:
      return text
  }
}
