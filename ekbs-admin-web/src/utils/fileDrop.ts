/**
 * 拖拽/选择文件夹时的**递归展开**（方案 05 §4.4 要求「选择文件」+「选择文件夹」）。
 *
 * 为什么必须递归：拖入一个文件夹时，浏览器给的是**一个目录 entry**，
 * 不是里面的文件。早先直接把 `dataTransfer.files` 当文件列表用，
 * 得到的是一条「名字是文件夹名、没有扩展名」的记录 —— 于是被类型校验判成
 * 「不支持的类型（无后缀）」，用户看到的就是「拖文件夹说类型不支持」。
 *
 * 两个坑：
 * 1. `readEntries` **每次最多返回 100 条**，必须循环读到返回空数组为止，
 *    否则大文件夹会静默少传（这是最经典的写法错误）；
 * 2. `dataTransfer.items` 在事件处理函数返回后就失效了，所以 entry 必须在
 *    同步阶段先取出来，再异步遍历。
 */

/** 只声明我们真正用到的那几个字段，避免依赖 DOM 的类型定义细节。 */
export interface FsEntry {
  isFile: boolean
  isDirectory: boolean
  fullPath?: string
  file?: (onOk: (f: File) => void, onErr?: (e: unknown) => void) => void
  createReader?: () => {
    readEntries: (onOk: (entries: FsEntry[]) => void, onErr?: (e: unknown) => void) => void
  }
}

/** 单次拖拽/选择允许的最大文件数（防止误拖一个几万文件的目录把后端打满）。 */
export const MAX_FOLDER_FILES = 300

/**
 * 全局遍历预算。**不能只靠目录深度护栏**：如果 `readEntries` 一直返回非空
 * （实现异常、或软链接成环），同层 `for(;;)` 永远退不出去 —— 实测就是这么把
 * 测试挂死的。所以给「访问过的 entry 总数」与「readEntries 调用次数」各设上限。
 */
export const MAX_ENTRIES = 5000
export const MAX_READ_CALLS = 500

export interface WalkBudget {
  entries: number
  reads: number
}

export function newBudget(): WalkBudget {
  return { entries: 0, reads: 0 }
}

export function budgetExhausted(b: WalkBudget, collected: number): boolean {
  return b.entries >= MAX_ENTRIES || b.reads >= MAX_READ_CALLS || collected >= MAX_FOLDER_FILES
}

/** 递归展开一组 entry；返回按遍历顺序去重前的文件列表。 */
export async function collectFilesFromEntries(
  entries: FsEntry[],
  out: File[] = [],
  depth = 0,
  budget: WalkBudget = newBudget(),
): Promise<File[]> {
  // 深度护栏 + 全局预算护栏，两者都要（深度只管纵向，预算管住横向爆炸）
  if (depth > 32) return out
  for (const entry of entries) {
    budget.entries += 1
    if (budgetExhausted(budget, out.length)) return out
    if (entry.isFile && entry.file) {
      const file = await new Promise<File | null>((resolve) => {
        entry.file!((f) => resolve(f), () => resolve(null))
      })
      if (file) {
        // 保留相对路径（folder/sub/a.xlsx），队列里显示它才好区分同名文件
        if (entry.fullPath && !(file as File & { webkitRelativePath?: string }).webkitRelativePath) {
          try {
            Object.defineProperty(file, 'webkitRelativePath', {
              value: entry.fullPath.replace(/^\//, ''),
              configurable: true,
            })
          } catch {
            /* 只读属性时忽略，显示回落到文件名 */
          }
        }
        out.push(file)
      }
      continue
    }
    if (entry.isDirectory && entry.createReader) {
      const reader = entry.createReader()
      // 必须循环读到空：浏览器每次最多给 100 条（大文件夹不会一次给全）
      for (;;) {
        budget.reads += 1
        if (budget.reads >= MAX_READ_CALLS || out.length >= MAX_FOLDER_FILES) break
        const batch = await new Promise<FsEntry[]>((resolve) => {
          reader.readEntries((entries) => resolve(entries), () => resolve([]))
        })
        if (!batch.length) break
        await collectFilesFromEntries(batch, out, depth + 1, budget)
      }
    }
  }
  return out
}

/** 从拖拽事件里取文件列表；支持拖文件夹（递归展开）。 */
export async function filesFromDataTransfer(dt: DataTransfer): Promise<File[]> {
  // 同步阶段先取 entry —— 异步之后 items 就失效了
  const entries: FsEntry[] = []
  for (const item of Array.from(dt.items ?? [])) {
    const maybe = (item as DataTransferItem & { webkitGetAsEntry?: () => FsEntry | null })
    const entry = maybe.webkitGetAsEntry?.()
    if (entry) entries.push(entry)
  }
  if (!entries.length) {
    // 老浏览器没有 webkitGetAsEntry：至少把普通文件接住
    return Array.from(dt.files ?? [])
  }
  return collectFilesFromEntries(entries)
}

/**
 * 按相对路径排序，让**上传顺序可预期**。
 *
 * `webkitdirectory` / 拖拽给出的文件顺序是操作系统目录顺序（不是文件名序），
 * 不排序的话 doc_id 的先后就是随机的 —— 用户会觉得"顺序乱了、跟文件夹里不一样"。
 * 用 `localeCompare('zh')` 才能让中文目录按拼音/笔画稳定排列，而不是按码位。
 */
export function sortByRelPath(files: File[]): File[] {
  return [...files].sort((a, b) => relPathOf(a).localeCompare(relPathOf(b), 'zh'))
}

/** 队列里显示/去重用的相对路径（文件夹上传时是 folder/sub/a.xlsx）。 */
export function relPathOf(file: File): string {
  const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath
  return rel && rel.length ? rel : file.name
}
