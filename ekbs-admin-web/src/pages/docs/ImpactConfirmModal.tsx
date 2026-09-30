/**
 * 破坏性操作二次确认（方案 05 §5.5 三级规范）。
 *
 * | 级别 | 判定 | 形式 |
 * |---|---|---|
 * | L-A 普通 | 可撤销、影响 ≤10 条 | 常规 Modal |
 * | L-B 中度 | 软删除或影响 11–200 条 | Modal + 影响清单 + 勾选「我已知晓影响」 |
 * | L-C 高危 | 不可逆、影响 >200 条、跨客体或权限/密级类 | Modal + 输入对象名**或**指定词 + 原因 ≥10 字 |
 *
 * 通用要求（§5.5 表格）在这里落地：
 * - 危险按钮在右且**默认不聚焦**（`autoFocusButton={null}`）；
 * - `Esc` 关闭 = 取消（Modal 默认键盘行为）；
 * - 提交态 `confirmLoading` 且禁用重复提交；
 * - 影响面数字由调用方从接口取真实值传入，本组件不生成任何数字。
 */
import { useEffect, useState, type ReactNode } from 'react'
import { Checkbox, Form, Input, Modal, Typography } from 'antd'

export type ConfirmLevel = 'A' | 'B' | 'C'

/** §5.5 L-C：原因至少 10 字。 */
export const MIN_REASON_LENGTH = 10

export interface ImpactConfirmModalProps {
  open: boolean
  level: ConfirmLevel
  title: string
  /** §5.4 破坏性确认句式：`确认{动作}「{对象名}」？此操作{后果}。` */
  consequence: ReactNode
  /** §5.5 L-B/L-C：影响清单摘要（调用方用真实数字渲染） */
  impact?: ReactNode
  /** L-C：要求输入的**指定词**（如 `DELETE` / `RESTORE`） */
  confirmWord?: string
  /**
   * L-C：**同时也接受**这些对象名（方案 05 §5.5 的原文是「输入对象名**或**指定词」）。
   *
   * 之前只传了 `confirmWord='DELETE'`，而界面文案写的是「请输入**文档名** `DELETE` 以确认」——
   * 文案与判定自相矛盾：用户照文案输入文档名，得到「与文档名不完全一致」且确认键永远灰着
   * （用户实测）。单篇删除时把标题传进来，两种输入都算数；批量删除才只认指定词。
   */
  confirmNames?: string[]
  /** L-C：原因必填且 ≥10 字 */
  requireReason?: boolean
  /** L-C 补充说明：当原因无法随请求提交时，如实告知而不是假装记录了 */
  reasonNote?: ReactNode
  /** 调用方的前置阻断（如超过后端单次批量上限） */
  okDisabled?: boolean
  loading?: boolean
  okText?: string
  onCancel: () => void
  onOk: (payload: { reason?: string }) => void
}

export default function ImpactConfirmModal({
  open,
  level,
  title,
  consequence,
  impact,
  confirmWord,
  confirmNames,
  requireReason = false,
  reasonNote,
  okDisabled = false,
  loading = false,
  okText,
  onCancel,
  onOk,
}: ImpactConfirmModalProps) {
  const [ack, setAck] = useState(false)
  const [word, setWord] = useState('')
  const [reason, setReason] = useState('')

  // 每次打开都从干净状态开始（避免上一次的勾选被复用）
  useEffect(() => {
    if (open) {
      setAck(false)
      setWord('')
      setReason('')
    }
  }, [open])

  const reasonOk = !requireReason || reason.trim().length >= MIN_REASON_LENGTH
  // 可接受的输入 = 指定词 ∪ 对象名（都按 trim 后精确比较；大小写不改，避免"差不多就行"）
  const accepted = [confirmWord, ...(confirmNames ?? [])].filter((v): v is string => Boolean(v))
  const wordOk = level !== 'C' || accepted.length === 0 || accepted.includes(word.trim())
  const nameOnly = !confirmWord && accepted.length > 0
  const ackOk = level === 'A' || ack
  const canOk = reasonOk && wordOk && ackOk && !okDisabled

  return (
    <Modal
      open={open}
      title={title}
      width={level === 'A' ? 420 : 520}
      okText={okText ?? (level === 'A' ? '确认' : '确认执行')}
      cancelText="取消"
      okButtonProps={{ danger: true, disabled: !canOk }}
      confirmLoading={loading}
      maskClosable={false}
      onCancel={onCancel}
      onOk={() => onOk(requireReason ? { reason: reason.trim() } : {})}
      destroyOnHidden
    >
      <Typography.Paragraph style={{ marginBottom: impact ? 12 : 0 }}>{consequence}</Typography.Paragraph>

      {impact ? <div style={{ marginBottom: 12 }}>{impact}</div> : null}

      {level === 'B' || level === 'C' ? (
        <Checkbox checked={ack} onChange={(e) => setAck(e.target.checked)}>
          我已知晓影响
        </Checkbox>
      ) : null}

      {level === 'C' ? (
        <Form layout="vertical" style={{ marginTop: 16 }}>
          {accepted.length ? (
            <Form.Item
              label={
                <span>
                  {nameOnly ? '请输入文档名 ' : '请输入 '}
                  {confirmWord ? <Typography.Text code>{confirmWord}</Typography.Text> : null}
                  {confirmWord && confirmNames?.length ? '，或文档名 ' : null}
                  {confirmNames?.length ? (
                    <Typography.Text code>{confirmNames.join('、')}</Typography.Text>
                  ) : null}
                  {' '}以确认
                </span>
              }
              required
              validateStatus={word && !wordOk ? 'error' : undefined}
              help={
                word && !wordOk
                  ? confirmNames?.length
                    ? `要一字不差（或输入 ${confirmWord}）`
                    : '与要求的内容不完全一致'
                  : undefined
              }
            >
              <Input
                value={word}
                onChange={(e) => setWord(e.target.value)}
                placeholder={confirmWord ?? confirmNames?.[0]}
                autoComplete="off"
              />
            </Form.Item>
          ) : null}

          {requireReason ? (
            <Form.Item
              label={`原因（至少 ${MIN_REASON_LENGTH} 字，写入审计）`}
              required
              validateStatus={reason && !reasonOk ? 'error' : undefined}
              help={
                reason && !reasonOk
                  ? `还需 ${MIN_REASON_LENGTH - reason.trim().length} 字`
                  : undefined
              }
            >
              <Input.TextArea
                rows={3}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                maxLength={200}
                showCount
                placeholder="例如：制度文件已废止，由新版替代"
              />
            </Form.Item>
          ) : null}

          {reasonNote ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {reasonNote}
            </Typography.Text>
          ) : null}
        </Form>
      ) : null}
    </Modal>
  )
}
