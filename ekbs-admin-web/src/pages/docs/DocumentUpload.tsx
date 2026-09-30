/**
 * 上传文档（方案 05 §4.4 上传抽屉线框图 + §6.5 大文件上传规范）。
 *
 * - 秒传：先 `POST /docs/upload/check-hash`，再 `init`（双保险）
 * - 断点续传：`GET /docs/upload/{id}/status` 的 `uploaded_chunks` 位图跳过已传分片
 * - 分片大小 / 并发 / 重试：取自 `/import/meta` 的 `upload.*`
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, AutoComplete, Button, Card, Form, Input, Modal, Progress, Select, Space, Table, Tag, Typography, message,
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
    { kb_id: number; code: string; name: string; default_level?: number; document_count?: number }[]
  >([])
  // 「加载中」与「加载完了但一个库都没有」必须分开：清库之后列表为空是**合法状态**，
  // 早期用 `loading={!kbs.length}` 会让首次启动永远转圈，看不到「去新建」的提示。
  const [kbLoaded, setKbLoaded] = useState(false)
  const [kbModalOpen, setKbModalOpen] = useState(false)
  const [kbSaving, setKbSaving] = useState(false)
  const [kbManageOpen, setKbManageOpen] = useState(false)
  const [concurrency, setConcurrency] = useState(3)
  const [maxRetry, setMaxRetry] = useState(3)
  const [chunkMb, setChunkMb] = useState(8)
  /** 当前知识库下已有的子目录（供「保存到文件夹」点选） */
  const [destFolders, setDestFolders] = useState<string[]>([])
  const [kbForm] = Form.useForm<{ code: string; name: string; default_level: number }>()
  const fileRef = useRef<HTMLInputElement>(null)
  const dirRef = useRef<HTMLInputElement>(null)
  const [form] = Form.useForm<{ kb_id?: number; level: number; dept_ids?: string; dest_folder?: string }>()

  const loadDestFolders = useCallback(async (kbId: number | undefined) => {
    const kb = kbs.find((k) => k.kb_id === kbId)
    if (!kb?.code) {
      setDestFolders([])
      return
    }
    try {
      // 落盘是 raw/<kb_code>/…；浏览该库根下的子目录供用户点选
      const res = (await http.get('/import/dirs', { params: { path: kb.code } })) as {
        dirs?: { name?: string; rel_path?: string }[]
      }
      const rows = res.dirs ?? []
      const opts = rows
        .map((r) => {
          const full = String(r.rel_path || r.name || '').replace(/\\/g, '/')
          // 去掉知识库 code 前缀，界面上显示相对库根的路径
          const prefix = `${kb.code}/`
          return full.startsWith(prefix) ? full.slice(prefix.length) : full === kb.code ? '' : (r.name || '')
        })
        .filter(Boolean)
      setDestFolders(opts)
    } catch {
      // 库根目录尚不存在（从未上传过）→ 空列表，仍可手填新路径
      setDestFolders([])
    }
  }, [kbs])

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
    const kbId = form.getFieldValue('kb_id') as number | undefined
    if (kbLoaded && kbId) void loadDestFolders(kbId)
  }, [kbLoaded, kbs, form, loadDestFolders])

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
    opts: { level: number; deptIds: string[]; kbId?: number; destFolder?: string },
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
    const deptIds = (values.dept_ids ?? '').split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)
    // L2/L3 必须给可见部门（方案 00 §3.2）：后端也会在 init 阶段拒，
    // 但在这里先拦能省一次往返，也不会留下"上传成功但解析失败"的困惑
    if (Number(values.level) >= 2 && deptIds.length === 0) {
      message.error(`密级 L${values.level} 必须填「可见部门 ID」（如 102）；L2/L3 不能不给部门。若要全公司可见请选 L1`)
      return
    }
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
            level: values.level,
            deptIds,
            kbId: values.kb_id,
            destFolder: values.dest_folder,
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
        width={660}
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

      <Card title="2. 目标与密级">
        <Form form={form} layout="vertical" initialValues={{ level: 2 }} style={{ maxWidth: 560 }}>
          <Form.Item label="目标知识库" required tooltip="上传到的知识库；没有合适的就点右边「新建」">
            <Space.Compact style={{ width: '100%' }}>
              <Form.Item
                name="kb_id"
                noStyle
                rules={[{ required: true, message: '请选择或新建一个知识库（留空会被拒 422）' }]}
              >
                <Select
                  style={{ width: 'calc(100% - 88px)' }}
                  placeholder={
                    !kbLoaded
                      ? '正在加载知识库列表…'
                      : kbs.length
                        ? '请选择知识库'
                        : '还没有知识库，点右边「新建」'
                  }
                  loading={!kbLoaded}
                  // 显示**名称**优先：code 是给命令行的，界面上不该拿它当主标识
                  options={kbs.map((k) => ({
                    value: k.kb_id,
                    label: `${k.name}${k.document_count ? `（${k.document_count} 篇）` : ''}`,
                    title: `code=${k.code}`,
                  }))}
                  onChange={(kbId: number) => {
                    // 知识库的「该库默认密级」= 上传页密级的初值（见 docUtils.initialLevelForKb）。
                    // 它不决定任何一份文档的真实密级：用户随后改「本文档密级」只改这一批文档。
                    const initial = initialLevelForKb(kbs, kbId)
                    if (initial) form.setFieldValue('level', initial)
                    form.setFieldValue('dest_folder', undefined)
                    void loadDestFolders(kbId)
                  }}
                />
              </Form.Item>
              <Button style={{ width: 88 }} onClick={() => setKbModalOpen(true)}>
                新建
              </Button>
              <Button style={{ width: 72 }} onClick={() => setKbManageOpen(true)}>
                管理
              </Button>
            </Space.Compact>
          </Form.Item>
          <Form.Item
            name="dest_folder"
            label="保存到文件夹"
            tooltip="相对知识库根目录。留空则落在库根；可点选已有目录，也可手填新路径（如 制度/2026）。最终路径：raw/<知识库>/<此文件夹>/<文件相对路径>。"
            normalize={(v) => String(v ?? '').trim().replace(/^\/+|\/+$/g, '').replace(/\\/g, '/') || undefined}
          >
            <AutoComplete
              allowClear
              placeholder="留空 = 知识库根目录；可点选或输入新路径（如 制度/2026）"
              options={destFolders.map((p) => ({ value: p, label: p }))}
              filterOption={(input, option) =>
                String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())
              }
            />
          </Form.Item>
          <Typography.Paragraph type="secondary" style={{ marginTop: -8, fontSize: 12 }}>
            「选择文件夹」是选<strong>本机待上传</strong>的源目录；这里的「保存到文件夹」才是服务器落盘位置。
          </Typography.Paragraph>
          <Form.Item
            name="level"
            label="本文档密级"
            tooltip="这一批上传的文档按它入库（每份文档可以不同，改的是这一批）。初值取自所选知识库的「该库默认密级」，不要就直接改这里 —— 改这里不会改知识库设置。L1 全公司可见；L2/L3 还必须填下面的部门。"
            rules={[{ required: true, message: '请选择密级' }]}
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
          <Form.Item name="dept_ids" label="可见部门 ID（逗号分隔）" tooltip="L1 可留空（全公司可见）；L2/L3 必填，否则不会入库。">
            <Input placeholder="102,103" />
          </Form.Item>
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
