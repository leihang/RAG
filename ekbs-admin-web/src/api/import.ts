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
