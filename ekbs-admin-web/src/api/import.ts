/**
 * 服务器目录导入（`ekbs/api/imports.py`）。全部端点都要 `doc:document:create`。
 *
 * 与上传（`doc.ts` 的 `/docs/upload/*`）的分工：文件**已经在服务器上**时走这条，
 * 只扫描 + 登记 `pending` 文档，解析交给 worker —— 网页请求不该挂几十分钟。
 */
import { http } from './client'
import type {
  ImportDirsResp,
  ImportMeta,
  ImportQueueResp,
  ImportRunBody,
  ImportRunResp,
  ImportScanBody,
  ImportScanResp,
} from './types'

/** 首屏元信息：raw 根、放行的后缀、知识库列表、上传默认值、入队上限。 */
export const getImportMeta = () => http.get('/import/meta') as Promise<ImportMeta>

/**
 * 浏览 raw 下的目录。
 *
 * `recursive=false`（默认）= 只列一层，返回 `{rel_path, parent, dirs, root}`，用于逐层进入；
 * `recursive=true` = 扁平列出全部子孙目录（上传页「保存到文件夹」下拉用的就是它）。
 */
export const listImportDirs = (path = '', recursive = false) =>
  http.get('/import/dirs', { params: { path, recursive } }) as Promise<ImportDirsResp>

/** 组织树里的一个部门。`dingtalk_dept_id` 就是文档上的可见部门 ID。 */
export interface DeptOption {
  dept_id: number
  dingtalk_dept_id: string
  name: string
  parent_dept_id: number | null
  tree_depth: number
  dept_code: string
}

/** 上传/导入选可见部门。权限与建目录相同（`doc:document:create`），审核员也能用。 */
export const listDeptOptions = () =>
  http.get('/import/depts') as Promise<{ items?: DeptOption[]; note?: string }>

/** 入库时可以额外点名的人。提交值是用户编号，不是部门 ID。 */
export interface ViewerOption {
  user_id: number
  nick: string
  title: string
  clearance: number
  /** 主部门。没有部门时为空，界面归到「未分配部门」。 */
  dept_id?: number | null
  dept_name: string
}

export const listViewerOptions = (q = '') =>
  http.get('/import/users', { params: q ? { q } : {} }) as Promise<{ items?: ViewerOption[] }>

/** 在 raw 下新建目录。带上密级时，把权限记在这个文件夹上。 */
export const createImportDir = (body: {
  path: string
  level?: number
  visible_dept_ids?: string[]
  viewer_user_ids?: number[]
}) =>
  http.post('/import/dirs', body) as Promise<{
    rel_path: string
    created: boolean
    folder_policy?: { level: number; visible_dept_ids: string[]; viewer_user_ids: number[] } | null
  }>

/** 删除空文件夹。磁盘上已经没有这个目录时也算成功。 */
export const deleteImportDir = (path: string) =>
  http.delete('/import/dirs', { params: { path }, skipErrorToast: true }) as Promise<{
    rel_path: string
    deleted: boolean
  }>

/** 预览：这个目录会导入哪些文件、哪些重复、有没有闸门挡着。**不写库**。 */
export const scanImportDir = (body: ImportScanBody) =>
  http.post('/import/scan', body) as Promise<ImportScanResp>

/**
 * 登记为待解析文档。`files` 非空 = 只导勾选的这些；**空数组 = 按目录全量**
 * （后端会自己重扫一遍拿全量可导入清单，不受预览 200 条截断影响）。
 */
export const runImport = (body: ImportRunBody) =>
  http.post('/import/run', body) as Promise<ImportRunResp>

/** 导入后的即时反馈：各状态计数 + 最近登记的文档。 */
export const getImportQueue = (limit = 20) =>
  http.get('/import/queue', { params: { limit } }) as Promise<ImportQueueResp>
