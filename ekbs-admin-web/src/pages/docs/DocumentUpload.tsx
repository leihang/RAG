/**
 * 上传文档（方案 05 §4.4 上传抽屉线框图 + §6.5 大文件上传规范）。
 *
 * - 秒传：先 `POST /docs/upload/check-hash`，再 `init`（双保险）
 * - 断点续传：`GET /docs/upload/{id}/status` 的 `uploaded_chunks` 位图跳过已传分片
 * - 分片大小 / 并发 / 重试：取自 `/import/meta` 的 `upload.*`
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Card, Form, Input, Modal, Progress, Select, Space, Table, Tag, TreeSelect, Typography, message,
} from 'antd'
import { InboxOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  updateKnowledgeBase,
  uploadCheckHash,
  uploadChunk,
  uploadComplete,
  uploadInit,
  uploadStatus,
} from '@/api/doc'
import { http } from '@/api/client'
import { buildKbTargetTree, formatKbTarget, isPersonalCode, parseKbTarget, personalOwnerId } from '@/components/kbTargetTree'
import { personalOwnerText } from '@/utils/personalKb'
import DeptPicker from '@/components/DeptPicker'
import ViewerPicker from '@/components/ViewerPicker'
import { formatBytes } from '@/utils/format'
import { formatExtList, normalizeExt } from '@/utils/fileExt'
import { MAX_FOLDER_FILES, filesFromDataTransfer, relPathOf, sortByRelPath } from '@/utils/fileDrop'
import { initialLevelForKb, joinUploadDest } from './docUtils'

/**
 * 准入格式**从后端取**（`/import/meta` 的 `upload_extensions` = 配置白名单 ∩ 解析器已实现）。
 *
 * 这里曾经硬编码 11 种 —— 等于又在"两个地方各写一份白名单"，和后端 CLI/配置
 * 那次"两张皮"是同一个错误。前端只做**即时提示**，准入的权威始终在后端（原则 P3）：
 * 拿不到列表时不拦（让后端 422 说话），而不是把用户的文件挡在本地。
 */
const MAX_MB = 500

type DestFolder = {
  path: string
  level: number | null
  visible_dept_ids: string[]
  viewer_user_ids: number[]
  policy_from: string | null
}
const FALLBACK_EXT = ['pdf', 'docx', 'doc', 'pptx', 'xlsx', 'xls', 'txt', 'md', 'png', 'jpg', 'jpeg']

type TaskStatus = 'pending' | 'waiting' | 'hashing' | 'uploading' | 'success' | 'instant' | 'failed'

interface UploadTask {
  key: string
  file: File
  /** 文件夹上传时的相对路径（folder/sub/a.xlsx），用于区分同名文件 */
  rel?: string
  status: TaskStatus
  percent: number
  error?: string
  note?: string
  docId?: number
}

/** 浏览器端 SHA-256（秒传用）。`crypto.subtle` 只在 https / localhost 可用，不可用则降级。 */
async function sha256(file: File): Promise<string | null> {
  try {
    if (!window.crypto?.subtle) return null
    if (file.size > 200 * 1024 * 1024) return null // 超大文件不做整文件摘要，避免阻塞
    const buf = await file.arrayBuffer()
    const digest = await crypto.subtle.digest('SHA-256', buf)
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

export default function DocumentUpload() {
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<UploadTask[]>([])
  const [running, setRunning] = useState(false)
  const [allowed, setAllowed] = useState<string[]>(FALLBACK_EXT)
  const [kbs, setKbs] = useState<
    { kb_id: number; code: string; name: string; default_level?: number; document_count?: number; owner_user_id?: number | null; owner_nick?: string | null; owner_dingtalk_userid?: string | null }[]
  >([])
  const [foldersByKb, setFoldersByKb] = useState<Record<number, DestFolder[]>>({})
  // 「加载中」与「加载完了但一个库都没有」必须分开：清库之后列表为空是**合法状态**，
  // 早期用 `loading={!kbs.length}` 会让首次启动永远转圈，看不到「去新建」的提示。
  const [kbLoaded, setKbLoaded] = useState(false)
  const [kbModalOpen, setKbModalOpen] = useState(false)
  const [kbSaving, setKbSaving] = useState(false)
  const [kbManageOpen, setKbManageOpen] = useState(false)
  const [concurrency, setConcurrency] = useState(3)
  const [maxRetry, setMaxRetry] = useState(3)
  const [chunkMb, setChunkMb] = useState(8)
  /** 各部门库下的文件夹，挂在目标知识库这棵折叠树上。 */
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [folderModalOpen, setFolderModalOpen] = useState(false)
  /** 非空时弹窗是在改已有文件夹，不再新建。 */
  const [folderEditPath, setFolderEditPath] = useState<string | null>(null)
  const [newFolderName, setNewFolderName] = useState('')
  const [newFolderLevel, setNewFolderLevel] = useState(2)
  const [newFolderDepts, setNewFolderDepts] = useState<string[]>([])
  const [newFolderViewers, setNewFolderViewers] = useState<number[]>([])
  const [kbForm] = Form.useForm<{ code: string; name: string; default_level: number }>()
  const fileRef = useRef<HTMLInputElement>(null)
  const dirRef = useRef<HTMLInputElement>(null)
  const [form] = Form.useForm<{
    kb_id?: number
    level: number
    dept_ids?: string[]
    dest_folder?: string
    viewer_user_ids?: number[]
  }>()
  const watchedKbId = Form.useWatch('kb_id', form)
  const watchedDest = Form.useWatch('dest_folder', form)

  const loadDestFolders = useCallback(async (kbId: number | undefined) => {
    const kb = kbs.find((k) => k.kb_id === kbId)
    if (!kb?.code || isPersonalCode(kb.code)) {
      if (kbId != null) setFoldersByKb((prev) => ({ ...prev, [kbId]: [] }))
      return
    }
    try {
      // 新库还没上传过时目录不存在，后端 404。这里自己当成空列表。
      const res = (await http.get('/import/dirs', {
        params: { path: kb.code, recursive: true },
        skipErrorToast: true,
      })) as {
        dirs?: {
          rel_under?: string
          rel_path?: string
          name?: string
          level?: number | null
          visible_dept_ids?: string[]
          viewer_user_ids?: number[]
          policy_from?: string | null
        }[]
      }
      const opts: DestFolder[] = (res.dirs ?? [])
        .map((row) => {
          const under = String(row.rel_under || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')
          const full = String(row.rel_path || row.name || '').replace(/\\/g, '/')
          const prefix = `${kb.code}/`
          const path = under || (full.startsWith(prefix) ? full.slice(prefix.length) : full)
          if (!path || path === kb.code) return null
          return {
            path,
            level: row.level ?? null,
            visible_dept_ids: row.visible_dept_ids ?? [],
            viewer_user_ids: row.viewer_user_ids ?? [],
            policy_from: row.policy_from ?? null,
          }
        })
        .filter((row): row is DestFolder => row != null)
        .sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'))
      setFoldersByKb((prev) => ({ ...prev, [kb.kb_id]: opts }))
      const selectedKb = form.getFieldValue('kb_id') as number | undefined
      const current = String(form.getFieldValue('dest_folder') ?? '')
      if (selectedKb === kb.kb_id && current && !opts.some((row) => row.path === current)) {
        form.setFieldValue('dest_folder', '')
      }
    } catch {
      setFoldersByKb((prev) => ({ ...prev, [kb.kb_id]: [] }))
      const selectedKb = form.getFieldValue('kb_id') as number | undefined
      const current = String(form.getFieldValue('dest_folder') ?? '')
      if (selectedKb === kb.kb_id && current) {
        form.setFieldValue('dest_folder', '')
      }
    }
  }, [kbs, form])

  const openFolderModal = (folder?: DestFolder) => {
    setFolderEditPath(folder?.path ?? null)
    setNewFolderName(folder?.path ?? '')
    setNewFolderLevel(folder?.level ?? 2)
    setNewFolderDepts(folder?.visible_dept_ids ?? [])
    setNewFolderViewers(folder?.viewer_user_ids ?? [])
    setFolderModalOpen(true)
  }

  const removeFolder = async (folderPath: string) => {
    const kbId = form.getFieldValue('kb_id') as number | undefined
    const kb = kbs.find((k) => k.kb_id === kbId)
    if (!kb?.code) return
    if (!window.confirm(`删除文件夹「${folderPath}」？里面有文件时会拒绝。`)) return
    try {
      await http.delete('/import/dirs', {
        params: { path: `${kb.code}/${folderPath}` },
        skipErrorToast: true,
      })
      message.success('已删除文件夹')
      if (String(form.getFieldValue('dest_folder') ?? '') === folderPath) {
        form.setFieldValue('dest_folder', '')
      }
      await loadDestFolders(kb.kb_id)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    }
  }

  const createFolderFromModal = async () => {
    const kbId = form.getFieldValue('kb_id') as number | undefined
    const kb = kbs.find((k) => k.kb_id === kbId)
    if (!kb?.code) {
      message.warning('请先选择目标知识库')
      return
    }
    const name = (folderEditPath ?? newFolderName).trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')
    if (!name) {
      message.warning('请填写文件夹名')
      return
    }
    const personal = kb.code.startsWith('personal-')
    if (!personal && newFolderLevel >= 2 && newFolderDepts.length === 0) {
      message.error(`密级 L${newFolderLevel} 必须选择可见部门`)
      return
    }
    setCreatingFolder(true)
    try {
      await http.post('/import/dirs', {
        path: `${kb.code}/${name}`,
        ...(personal ? {} : {
          level: newFolderLevel,
          visible_dept_ids: newFolderDepts,
          viewer_user_ids: newFolderViewers,
        }),
      }, { skipErrorToast: true })
      message.success(folderEditPath ? `已更新文件夹 ${name} 的权限` : `已建立文件夹 ${name}`)
      setFolderModalOpen(false)
      setFolderEditPath(null)
      setNewFolderName('')
      await loadDestFolders(kb.kb_id)
      form.setFieldValue('dest_folder', name)
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setCreatingFolder(false)
    }
  }

  useEffect(() => {
    // 准入清单与知识库列表都以后端为准。
    // 知识库必须是**选择**而不是空文本框：后端在 complete 阶段要求
    // `kb_id` 或 `kb_code` 至少给一个，留空直接 422。
    http
      .get('/import/meta')
      .then((meta) => {
        const m = meta as {
          upload_extensions?: string[]
          knowledge_bases?: {
            kb_id: number
            code: string
            name: string
            default_level?: number
            document_count?: number
          }[]
          upload?: { chunk_size_mb?: number; concurrent?: number; max_retry?: number }
        }
        // 并发与重试的默认值来自服务端配置（方案 §6.5 的 upload.*），
        // 界面上只是"允许临时调小"，不再写死
        if (m.upload?.concurrent) setConcurrency(m.upload.concurrent)
        if (m.upload?.max_retry) setMaxRetry(m.upload.max_retry)
        if (m.upload?.chunk_size_mb) setChunkMb(m.upload.chunk_size_mb)
        if (m.upload_extensions?.length) setAllowed(m.upload_extensions)
        const list = m.knowledge_bases ?? []
        setKbs(list)
        setKbLoaded(true)
        // 空列表是合法状态（首次部署、清库后）：此时不设置 kb_id，
        // 由必填校验提示用户去点「新建」，而不是卡在"正在加载"上。
        if (list.length) {
          const first = list[0].kb_id
          form.setFieldValue('kb_id', first)
          // ⚠️ `setFieldValue` **不触发 onChange**，所以初值必须显式跟 onChange 走同一
          // 个口径 —— 否则"选库带出该库默认密级"只在用户手动切换时生效，
          // 首屏却是表单写死的 L2（而 L2 必填部门），同一件事两个结果。
          const initial = initialLevelForKb(list, first)
          if (initial) form.setFieldValue('level', initial)
        }
      })
      .catch(() => setKbLoaded(true))
  }, [form])

  useEffect(() => {
    if (!kbLoaded) return
    for (const kb of kbs) {
      if (!isPersonalCode(kb.code)) void loadDestFolders(kb.kb_id)
    }
  }, [kbLoaded, kbs, loadDestFolders])

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState !== 'visible') return
      const kbId = form.getFieldValue('kb_id') as number | undefined
      if (kbId) void loadDestFolders(kbId)
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [form, loadDestFolders])

  const patch = useCallback((key: string, delta: Partial<UploadTask>) => {
    setTasks((prev) => prev.map((t) => (t.key === key ? { ...t, ...delta } : t)))
  }, [])

  const addFiles = (input: FileList | File[] | null, source: 'file' | 'dir' | 'drop' = 'file') => {
    // 先按相对路径排序：拖文件夹时浏览器给的是目录顺序（非文件名序），
    // 不排序的话 doc_id 顺序随机，用户会觉得「顺序乱了」
    const list = sortByRelPath(Array.from(input ?? []))
    if (!list.length) return
    const accepted: UploadTask[] = []
    let skippedType = 0
    let skippedSize = 0
    let extensionless = 0
    for (const file of list) {
      const ext = normalizeExt(file.name)
      // allowed 为空（还没拿到后端列表）时不拦：准入由后端决定
      if (allowed.length && !allowed.includes(ext)) {
        skippedType += 1
        if (!ext) extensionless += 1
        // 只对前 3 个逐条提示，避免一个几百文件的目录刷屏
        if (skippedType <= 3) {
          message.warning(
            `${relPathOf(file)}：不支持的类型「.${ext || '（无后缀）'}」；支持 ${formatExtList(allowed)}`,
          )
        }
        continue
      }
      if (file.size > MAX_MB * 1024 * 1024) {
        skippedSize += 1
        if (skippedSize <= 3) message.warning(`${relPathOf(file)}：超过单文件 ${MAX_MB} MB 限制`)
        continue
      }
      const rel = relPathOf(file)
      accepted.push({ key: `${rel}-${file.size}-${file.lastModified}`, file, status: 'pending', percent: 0, rel })
    }
    if (skippedType > 3) message.warning(`另有 ${skippedType - 3} 个文件因类型不支持被跳过`)
    if (skippedSize > 3) message.warning(`另有 ${skippedSize - 3} 个文件因超过 ${MAX_MB} MB 被跳过`)
    if (extensionless && source === 'drop') {
      // 老浏览器把文件夹当成"没有后缀的文件"给过来，这里给出可执行的下一步
      message.info('提示：拖入的是文件夹？若浏览器不支持目录拖拽，请用「选择文件夹」按钮。')
    }
    if (accepted.length) {
      setTasks((prev) => {
        const merged = [...prev, ...accepted]
        if (merged.length > MAX_FOLDER_FILES) {
          message.warning(
            `一次最多 ${MAX_FOLDER_FILES} 个文件（当前 ${merged.length}），已保留前 ${MAX_FOLDER_FILES} 个；请分批上传`,
          )
          return merged.slice(0, MAX_FOLDER_FILES)
        }
        return merged
      })
    }
  }

  /** 拖拽：可能是文件，也可能是**文件夹**（递归展开）。 */
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    try {
      const files = await filesFromDataTransfer(e.dataTransfer)
      if (!files.length) {
        message.info('没有从拖入内容里读到文件')
        return
      }
      addFiles(files, 'drop')
    } catch (err) {
      message.error(`读取拖入内容失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  const uploadOne = async (
    task: UploadTask,
    opts: { level: number; deptIds: string[]; kbId?: number; destFolder?: string; viewerIds: number[] },
  ) => {
    patch(task.key, { status: 'hashing', percent: 0 })
    const checksum = await sha256(task.file)
    const fileRel = relPathOf(task.file)
    // 用户选的「保存到文件夹」= 知识库内相对路径前缀；与拖拽带来的相对路径拼接
    const rel = joinUploadDest(opts.destFolder, fileRel)
    if (checksum) {
      const hit = await uploadCheckHash(checksum, rel, task.file.size)
      if (hit.instant && hit.doc_id != null) {
        patch(task.key, { status: 'instant', percent: 100, docId: hit.doc_id })
        return
      }
    }
    const init = await uploadInit({
      // 传**相对路径**：后端会保留目录层级落到 raw 下，并把它作为标题，
      // 否则 `Path(filename).stem` 会把文件夹整段丢掉（用户反馈「文件夹被分离了」）
      filename: rel,
      title: rel.replace(/\.[^.]+$/, ''),
      size_bytes: task.file.size,
      checksum,
      kb_id: opts.kbId ?? null,
      level: opts.level,
      visible_dept_ids: opts.deptIds,
      viewer_user_ids: opts.viewerIds,
    })
    if (init.instant) {
      patch(task.key, { status: 'instant', percent: 100, docId: init.doc_id })
      return
    }
    const size = init.chunk_size_hint || task.file.size
    const total = Math.max(1, Math.ceil(task.file.size / size))
    // 断点续传：合并 init 返回的位图与 status 接口（刷新/重试时 status 更准）
    let doneSet = new Set<number>(init.uploaded_chunks ?? [])
    try {
      const st = await uploadStatus(init.upload_id)
      doneSet = new Set(st.uploaded_chunks ?? [])
    } catch {
      // status 失败不阻断：仍按 init 位图继续
    }
    for (let i = 0; i < total; i += 1) {
      if (doneSet.has(i)) {
        patch(task.key, {
          status: 'uploading',
          percent: Math.round(((i + 1) / total) * 100),
          note: '跳过已传分片',
        })
        continue
      }
      const blob = task.file.slice(i * size, Math.min((i + 1) * size, task.file.size))
      // 失败分片**指数退避重试**（方案 §6.5：重试 3 次）。没有这一步的话，
      // 一次网络抖动就要用户手动重传整个文件。
      let lastErr: unknown = null
      for (let attempt = 0; attempt <= maxRetry; attempt += 1) {
        try {
          await uploadChunk(init.upload_id, i, blob, `${task.file.name}.part${i}`)
          lastErr = null
          break
        } catch (err) {
          lastErr = err
          if (attempt < maxRetry) {
            const wait = 500 * 2 ** attempt
            patch(task.key, { note: `第 ${attempt + 1} 次失败，${wait}ms 后重试…` })
            await new Promise((r) => setTimeout(r, wait))
          }
        }
      }
      if (lastErr) throw lastErr
      patch(task.key, { status: 'uploading', percent: Math.round(((i + 1) / total) * 100), note: '' })
    }
    const done = await uploadComplete(init.upload_id)
    patch(task.key, { status: 'success', percent: 100, docId: done.doc_id })
  }

  const startAll = async () => {
    const values = await form.validateFields()
    if (!values.kb_id) { message.warning('请先选择目标知识库'); return }
    const target = kbs.find((k) => k.kb_id === values.kb_id)
    const personal = isPersonalCode(target?.code)
    const destFolders = foldersByKb[values.kb_id] ?? []
    const dest = personal ? '' : String(values.dest_folder ?? '').replace(/^\/+|\/+$/g, '')
    const folder = destFolders.find((row) => row.path === dest)
    const hasOwnDept = (folder?.visible_dept_ids.length ?? 0) > 0
    if (!personal && !hasOwnDept) {
      message.error(dest
        ? '这个文件夹还没有自己的部门和密级。点「修改权限」写好后再上传。'
        : '请先新建文件夹，在弹窗里写上密级和部门，再选中它上传。')
      return
    }
    const ownerId = personalOwnerId(target) ?? 0
    const rawDepts = personal
      ? [`user:${ownerId}`]
      : (hasOwnDept ? folder?.visible_dept_ids : values.dept_ids)
    const deptIds = (Array.isArray(rawDepts) ? rawDepts : String(rawDepts ?? '').split(/[,，\s]+/))
      .map((s) => String(s).trim())
      .filter(Boolean)
    const level = personal ? 2 : (hasOwnDept ? Number(folder?.level) : Number(values.level))
    // L2/L3 必须给可见部门（方案 00 §3.2）：后端也会在 init 阶段拒，
    // 但在这里先拦能省一次往返，也不会留下"上传成功但解析失败"的困惑
    if (personal && !(ownerId > 0)) {
      message.error('这个个人库没有归属用户，不能投喂')
      return
    }
    if (!personal && level >= 2 && deptIds.length === 0) {
      message.error(`密级 L${level} 必须选择可见部门；L2/L3 不能不给部门。若要全公司可见请选 L1`)
      return
    }
    const viewerIds = personal
      ? []
      : (hasOwnDept ? (folder?.viewer_user_ids ?? []) : (values.viewer_user_ids ?? []))
        .map((id) => Number(id)).filter((id) => Number.isFinite(id) && id > 0)
    setRunning(true)
    // **并发池**：同时最多跑 `concurrency` 个文件（方案 §6.5 默认 3）。
    // 原来是 for-await 串行，界面上的「并发数」形同虚设。
    const queue = tasks.filter((t) => t.status === 'pending' || t.status === 'failed')
    for (const t of queue) patch(t.key, { status: 'waiting', note: '排队中' })
    let cursor = 0
    const worker = async () => {
      for (;;) {
        const idx = cursor
        cursor += 1
        const task = queue[idx]
        if (!task) return
        try {
          await uploadOne(task, {
            level,
            deptIds,
            kbId: values.kb_id,
            destFolder: dest,
            viewerIds,
          })
        } catch (err) {
          // 单文件失败不影响其他文件（§6.5 失败隔离）
          patch(task.key, {
            status: 'failed',
            note: '',
            error: err instanceof Error ? err.message : String(err),
          })
        }
      }
    }
    try {
      await Promise.all(
        Array.from({ length: Math.max(1, Math.min(concurrency, queue.length)) }, () => worker()),
      )
    } finally { setRunning(false) }
  }

  const folderCount = useMemo(
    () => new Set(tasks.map((t) => { const r = relPathOf(t.file); const i = r.lastIndexOf('/'); return i > 0 ? r.slice(0, i) : '' }).filter(Boolean)).size,
    [tasks],
  )
  const okCount = useMemo(() => tasks.filter((t) => t.status === 'success' || t.status === 'instant').length, [tasks])
  const failCount = useMemo(() => tasks.filter((t) => t.status === 'failed').length, [tasks])
  const targetTree = useMemo(() => buildKbTargetTree(
    kbs,
    Object.fromEntries(Object.entries(foldersByKb).map(([id, rows]) => [
      Number(id),
      rows.map((folder) => {
        const deptCount = folder.visible_dept_ids.length
        const mark = folder.level == null
          ? '未设置'
          : deptCount === 0
            ? `密级沿用上级 L${folder.level}，部门未设置`
            : `L${folder.level} · 部门 ${deptCount} 个 · 额外 ${folder.viewer_user_ids.length} 人`
        return { path: folder.path, mark }
      }),
    ])),
    personalOwnerText,
  ), [kbs, foldersByKb])
  const destFolders = foldersByKb[watchedKbId ?? -1] ?? []
  const chooseTarget = (raw: string | undefined) => {
    const parsed = parseKbTarget(raw)
    if (!parsed) {
      form.setFieldValue('kb_id', undefined)
      form.setFieldValue('dest_folder', '')
      return
    }
    const kb = kbs.find((item) => item.kb_id === parsed.kbId)
    const previous = form.getFieldValue('kb_id') as number | undefined
    form.setFieldValue('kb_id', parsed.kbId)
    form.setFieldValue('dest_folder', isPersonalCode(kb?.code) ? '' : parsed.dest)
    if (previous !== parsed.kbId && kb && !isPersonalCode(kb.code)) {
      const initial = initialLevelForKb(kbs, parsed.kbId)
      if (initial) form.setFieldValue('level', initial)
    }
  }

  const submitKb = async () => {
    const v = await kbForm.validateFields()
    setKbSaving(true)
    try {
      const created = await createKnowledgeBase(v)
      message.success(`已创建知识库「${created.name}」`)
      setKbs((prev) => [...prev, created])
      form.setFieldValue('kb_id', created.kb_id)
      setKbModalOpen(false)
      kbForm.resetFields()
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err))
    } finally {
      setKbSaving(false)
    }
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="本页从浏览器上传文件"
        description={
          <span>
            若文件已在服务器 raw 目录，请用{' '}
            <a onClick={() => navigate('/docs/import')}>目录导入</a>
            （批量登记进待解析队列，不经过本机上传）。
          </span>
        }
      />
      <Modal
        title="新建知识库"
        open={kbModalOpen}
        onCancel={() => setKbModalOpen(false)}
        onOk={() => void submitKb()}
        confirmLoading={kbSaving}
        destroyOnClose
      >
        <Form form={kbForm} layout="vertical" initialValues={{ default_level: 2 }}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '必填' }]}
            extra="界面与引用里显示的就是它，用中文即可"
          >
            <Input placeholder="如：制度与流程库" />
          </Form.Item>
          <Form.Item
            name="code"
            label="code"
            rules={[
              { required: true, message: '必填' },
              { pattern: /^[a-z0-9][a-z0-9_-]{1,31}$/, message: '只能用小写字母/数字/下划线/连字符，2–32 位' },
            ]}
            extra="给命令行与接口用的稳定标识；建成后不可修改（历史记录与接口路径里用的就是它）"
          >
            <Input placeholder="如：policy" />
          </Form.Item>
          <Form.Item
            name="default_level"
            label="该库默认密级"
            rules={[{ required: true }]}
            extra="只决定「选了这个库之后」上传页密级的初值，省一次手选；每份文档的真实密级在上传时单独确定，可以改。"
          >
            <Select
              options={[
                { value: 1, label: 'L1 公开（全公司可见）' },
                { value: 2, label: 'L2 内部（须指定部门）' },
                { value: 3, label: 'L3 秘密（须指定部门）' },
                { value: 4, label: 'L4 机密（首期不入库）' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={folderEditPath ? '修改文件夹权限' : '新建文件夹'}
        open={folderModalOpen}
        onCancel={() => { setFolderModalOpen(false); setFolderEditPath(null) }}
        onOk={() => void createFolderFromModal()}
        confirmLoading={creatingFolder}
        okText={folderEditPath ? '保存' : '建立并选中'}
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Input
            value={newFolderName}
            placeholder="例如 工程部"
            disabled={folderEditPath != null}
            onChange={(e) => setNewFolderName(e.target.value)}
          />
          {kbs.some((k) => k.kb_id === watchedKbId && k.code.startsWith('personal-')) ? (
            <Typography.Text type="secondary">个人库文件夹不另设密级和部门。</Typography.Text>
          ) : (
            <>
              <Select
                style={{ width: '100%' }}
                value={newFolderLevel}
                onChange={setNewFolderLevel}
                options={[
                  { value: 1, label: 'L1 公开（全公司可见）' },
                  { value: 2, label: 'L2 内部（须指定部门）' },
                  { value: 3, label: 'L3 秘密（须指定部门）' },
                  { value: 4, label: 'L4 机密（首期不入库）' },
                ]}
              />
              <DeptPicker value={newFolderDepts} onChange={setNewFolderDepts} />
              <ViewerPicker value={newFolderViewers} onChange={setNewFolderViewers} />
              <Typography.Text type="secondary">
                密级、部门和额外可查看的人记在这个文件夹上。之后传到这里，不用再选一遍。
              </Typography.Text>
            </>
          )}
        </Space>
      </Modal>
      <Alert
        type="info"
        showIcon
        message="上传后由 worker 异步解析"
        description={
          <>
            支持 {formatExtList(allowed)}；单文件 ≤ {MAX_MB} MB；大于 8 MB 自动分片（可配）。
            <br />
            秒传走 check-hash；断点续传按已传分片位图跳过；分片失败会指数退避重试。
          </>
        }
      />

      <Modal
        title="管理知识库"
        open={kbManageOpen}
        onCancel={() => setKbManageOpen(false)}
        footer={null}
        width={900}
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          code 是给命令行与接口用的稳定标识，建成后不可改（历史记录里记的是它）；名称随时可改。
          删除只在库为空时允许（避免误删整库语料）。
        </Typography.Paragraph>
        <Table
          rowKey="kb_id"
          size="small"
          pagination={false}
          dataSource={kbs}
          columns={[
            {
              title: '名称', dataIndex: 'name', width: 220,
              render: (v: string, r: { kb_id: number; name: string }) => (
                <Typography.Text
                  editable={{
                    onChange: (next: string) => {
                      const name = next.trim()
                      if (!name || name === r.name) return
                      void updateKnowledgeBase(r.kb_id, { name })
                        .then(() => {
                          message.success('已改名')
                          setKbs((prev) => prev.map((k) => (k.kb_id === r.kb_id ? { ...k, name } : k)))
                        })
                        .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
                    },
                  }}
                >
                  {v}
                </Typography.Text>
              ),
            },
            { title: 'code', dataIndex: 'code', width: 120 },
            {
              title: '归属',
              width: 220,
              ellipsis: true,
              render: (_: unknown, r: { code: string; owner_user_id?: number | null; owner_nick?: string | null; owner_dingtalk_userid?: string | null }) =>
                personalOwnerText(r) || '—',
            },
            { title: '文档数', dataIndex: 'document_count', width: 80, render: (v: number) => v ?? 0 },
            { title: '默认密级', dataIndex: 'default_level', width: 90, render: (v: number) => `L${v}` },
            {
              title: '操作', width: 80,
              render: (_: unknown, r: { kb_id: number; name: string }) => (
                <a
                  style={{ color: '#ff4d4f' }}
                  onClick={() => {
                    if (!window.confirm(`确认删除知识库「${r.name}」？库里有文档时会被拒。`)) return
                    void deleteKnowledgeBase(r.kb_id)
                      .then(() => {
                        message.success('已删除')
                        setKbs((prev) => prev.filter((k) => k.kb_id !== r.kb_id))
                      })
                      .catch((e) => message.error(e instanceof Error ? e.message : String(e)))
                  }}
                >
                  删除
                </a>
              ),
            },
          ]}
        />
      </Modal>

      <Card title="1. 选文件">
        <div
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => void onDrop(e)}
          style={{ border: '1.5px dashed #d9d9d9', borderRadius: 8, padding: '28px 16px', textAlign: 'center', background: '#fafafa' }}
        >
          <InboxOutlined style={{ fontSize: 36, color: '#1677ff' }} />
          <div style={{ marginTop: 8 }}><b>将文件或文件夹拖拽到此处</b></div>
          <div style={{ color: 'rgba(0,0,0,.45)', fontSize: 12, marginTop: 4 }}>
            支持拖入整个文件夹（会递归展开）；一次最多 {MAX_FOLDER_FILES} 个文件
          </div>
          <Space style={{ marginTop: 12 }}>
            <Button onClick={() => fileRef.current?.click()}>选择文件</Button>
            <Button onClick={() => dirRef.current?.click()}>选择文件夹</Button>
          </Space>
          <input
            ref={fileRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => { addFiles(e.target.files, 'file'); e.target.value = '' }}
          />
          {/* webkitdirectory 是浏览器原生的目录选择器；React 类型里没有，用对象展开绕过 */}
          <input
            ref={dirRef}
            type="file"
            multiple
            {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            style={{ display: 'none' }}
            onChange={(e) => { addFiles(e.target.files, 'dir'); e.target.value = '' }}
          />
        </div>
      </Card>

      <Card title="2. 目标">
        <Form form={form} layout="vertical" initialValues={{ level: 2, dest_folder: '' }} style={{ maxWidth: 720 }}>
          <Form.Item label="目标知识库" required tooltip="部门库展开后选择文件夹。个人库收在最下面，选中后直接投喂。">
            <Space.Compact style={{ width: '100%' }}>
              <TreeSelect
                style={{ width: 'calc(100% - 160px)' }}
                treeData={targetTree}
                treeDefaultExpandAll={false}
                treeNodeFilterProp="title"
                showSearch
                allowClear
                loading={!kbLoaded}
                styles={{ popup: { root: { maxHeight: 360, overflow: 'auto' } } }}
                placeholder={
                  !kbLoaded
                    ? '正在加载知识库列表…'
                    : kbs.length
                      ? '展开后选择知识库或文件夹'
                      : '还没有知识库，点右边「新建」'
                }
                value={formatKbTarget(watchedKbId, watchedDest)}
                onChange={(raw) => chooseTarget(raw == null ? undefined : String(raw))}
              />
              <Button style={{ width: 88 }} onClick={() => setKbModalOpen(true)}>
                新建
              </Button>
              <Button style={{ width: 72 }} onClick={() => setKbManageOpen(true)}>
                管理
              </Button>
            </Space.Compact>
          </Form.Item>
          <Form.Item
            name="kb_id"
            hidden
            rules={[{ required: true, message: '请选择或新建一个知识库（留空会被拒 422）' }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="dest_folder" hidden>
            <Input />
          </Form.Item>
          {(() => {
            const kb = kbs.find((item) => item.kb_id === watchedKbId)
            if (!kb) return null
            const personal = isPersonalCode(kb?.code)
            if (personal) {
              return (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={`投喂到${personalOwnerText(kb ?? {}) || '这个人'}的私有库`}
                  description="只有本人能检索。不用再选文件夹、密级、部门或额外可查看的人。"
                />
              )
            }
            const dest = String(watchedDest ?? '')
            const folder = destFolders.find((row) => row.path === dest)
            const ready = (folder?.visible_dept_ids.length ?? 0) > 0
            return (
              <>
                <Space style={{ marginBottom: 8 }}>
                  <Button
                    htmlType="button"
                    onClick={() => {
                      const kbId = form.getFieldValue('kb_id') as number | undefined
                      void loadDestFolders(kbId)
                    }}
                  >
                    刷新
                  </Button>
                  <Button htmlType="button" onClick={() => openFolderModal()}>
                    新建文件夹
                  </Button>
                  {folder ? (
                    <Button htmlType="button" danger onClick={() => void removeFolder(folder.path)}>
                      删除当前文件夹
                    </Button>
                  ) : null}
                </Space>
                {folder && ready ? (
                  <Alert
                    type="success"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message={`按文件夹「${folder.path}」入库：L${folder.level}，可见部门 ${folder.visible_dept_ids.length} 个，额外可查看 ${folder.viewer_user_ids.length} 人`}
                    description="权限只记在文件夹上，这里不再选一遍。"
                    action={(
                      <Button htmlType="button" size="small" onClick={() => openFolderModal(folder)}>
                        修改权限
                      </Button>
                    )}
                  />
                ) : (
                  <Alert
                    type="info"
                    showIcon
                    style={{ marginBottom: 16 }}
                    message={folder ? `文件夹「${folder.path}」还没有自己的部门` : '先新建或选中文件夹'}
                    description={folder
                      ? '密级和部门在「修改权限」里写。不会沿用上级文件夹的部门。'
                      : '密级、部门和额外可查看的人只在新建文件夹时填写。'}
                    action={folder ? (
                      <Button htmlType="button" size="small" onClick={() => openFolderModal(folder)}>
                        修改权限
                      </Button>
                    ) : undefined}
                  />
                )}
              </>
            )
          })()}
          <Form.Item
            label="并发数"
            tooltip="同时上传几个文件。同一个文件是切片后依次传的，这里不控制分片大小。调大上传更快，但同时占用更多带宽；网络不稳时调小更稳。"
          >
            <Select
              value={concurrency}
              onChange={setConcurrency}
              style={{ width: 140 }}
              options={[1, 2, 3, 4, 5].map((n) => ({
                value: n,
                label: `${n} 个文件${n === 3 ? '（默认）' : ''}`,
              }))}
            />
          </Form.Item>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
            单个文件超过 {chunkMb} MB 会自动切片传输；某一篇切片失败会自动重试 {maxRetry} 次。
          </Typography.Paragraph>
        </Form>
      </Card>

      <Card
        title={
          `3. 上传队列（${tasks.length} 个文件` +
          (folderCount > 1 ? ` · 来自 ${folderCount} 个文件夹` : folderCount === 1 ? ' · 来自 1 个文件夹' : '') +
          '）'
        }
        extra={
          <Space>
            <span>成功 {okCount} 失败 {failCount} 等待 {tasks.length - okCount - failCount}</span>
            <Button type="primary" loading={running} disabled={!tasks.length} onClick={() => void startAll()}>开始上传</Button>
            <Button onClick={() => setTasks([])} disabled={running || !tasks.length}>清空队列</Button>
            {okCount > 0 ? <Button onClick={() => navigate('/parse/tasks')}>去解析任务中心</Button> : null}
          </Space>
        }
      >
        <Table<UploadTask>
          rowKey="key"
          dataSource={tasks}
          pagination={false}
          size="small"
          locale={{ emptyText: '尚未选择文件' }}
          columns={[
            // **显示"来自哪个文件夹"**：拖文件夹时原来只列文件，
            // 看不出它是哪个目录来的、也看不出总共拖了几个目录
            { title: '来源文件夹', width: 180,
              render: (_, r) => {
                const rel = relPathOf(r.file)
                const idx = rel.lastIndexOf('/')
                return idx > 0
                  ? <Typography.Text ellipsis style={{ maxWidth: 170 }} title={rel.slice(0, idx)}>
                      📁 {rel.slice(0, idx)}
                    </Typography.Text>
                  : <Typography.Text type="secondary">（单独选择）</Typography.Text>
              } },
            { title: '文件', dataIndex: 'file',
              render: (_, r) => {
                const rel = relPathOf(r.file)
                const idx = rel.lastIndexOf('/')
                return (
                  <Typography.Text ellipsis style={{ maxWidth: 240 }} title={rel}>
                    {idx > 0 ? rel.slice(idx + 1) : rel}
                  </Typography.Text>
                )
              } },
            // 按大小自动选单位：843 KB / 1.2 MB / 512 B（原来一律 KB，大文件不直观）
            { title: '大小', width: 100, render: (_, r) => formatBytes(r.file.size) },
            {
              title: '进度', width: 220,
              render: (_, r) => <Progress percent={r.percent} size="small" status={r.status === 'failed' ? 'exception' : undefined} />,
            },
            {
              title: '状态', width: 150,
              render: (_, r) => {
                const map: Record<TaskStatus, [string, string]> = {
                  pending: ['default', '待开始'], waiting: ['default', '排队中'], hashing: ['processing', '计算摘要'],
                  uploading: ['processing', '上传中'], success: ['success', '已登记'],
                  instant: ['success', '秒传命中'], failed: ['error', '失败'],
                }
                const [color, text] = map[r.status]
                return (
                  <>
                    <Tag color={color}>{text}</Tag>
                    {r.note ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{r.note}</Typography.Text> : null}
                    {r.error ? <Typography.Text type="danger" style={{ fontSize: 12 }}>{r.error}</Typography.Text> : null}
                  </>
                )
              },
            },
            {
              title: '文档', width: 90,
              render: (_, r) => (r.docId ? <a onClick={() => navigate(`/docs/${r.docId}`)}>#{r.docId}</a> : '-'),
            },
            {
              title: '操作', width: 80,
              render: (_, r) => (r.status === 'pending' || r.status === 'failed'
                ? <a onClick={() => setTasks((p) => p.filter((t) => t.key !== r.key))}>移除</a> : '-'),
            },
          ]}
        />
      </Card>
    </Space>
  )
}
