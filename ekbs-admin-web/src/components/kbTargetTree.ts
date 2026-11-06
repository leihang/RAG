/**
 * 上传页「目标知识库」的折叠树。
 * 部门库可以展开到文件夹；个人库收在「个人知识库」下，选中后不再另选密级。
 */

export type KbTargetRow = {
  kb_id: number
  code: string
  name: string
  document_count?: number
  owner_user_id?: number | null
  owner_nick?: string | null
  owner_dingtalk_userid?: string | null
}

export type KbFolderMark = {
  path: string
  mark: string
}

export type KbTargetNode = {
  title: string
  value: string
  selectable?: boolean
  children?: KbTargetNode[]
}

export function isPersonalCode(code: string | undefined): boolean {
  return Boolean(code?.startsWith('personal-'))
}

/** 个人库主人。接口没带 owner 时，从代号 personal-5 里取。 */
export function personalOwnerId(row: { code?: string; owner_user_id?: number | null } | undefined): number | null {
  if (!row || !isPersonalCode(row.code)) return null
  if (row.owner_user_id) return Number(row.owner_user_id)
  const matched = /^personal-(\d+)$/.exec(row.code ?? '')
  return matched ? Number(matched[1]) : null
}

/** 个人原件在 raw 下的相对根：`personal/<uid>`（与钉钉投递一致，不是 `personal-<uid>`）。 */
export function personalRawRel(code: string | undefined): string | null {
  const owner = personalOwnerId({ code })
  return owner != null ? `personal/${owner}` : null
}

/** 知识库在 raw 下的目录前缀：个人库走 personal/<uid>，部门库仍用 code。 */
export function rawRootForKb(code: string | undefined): string {
  return personalRawRel(code) ?? String(code ?? '')
}

export function formatKbTarget(kbId: number | undefined, dest?: string): string | undefined {
  if (kbId == null) return undefined
  const folder = String(dest ?? '').replace(/^\/+|\/+$/g, '')
  return folder ? `kb:${kbId}|${folder}` : `kb:${kbId}`
}

export function parseKbTarget(value: string | undefined | null): { kbId: number; dest: string } | null {
  const matched = /^kb:(\d+)(?:\|(.*))?$/.exec(String(value ?? ''))
  if (!matched) return null
  return { kbId: Number(matched[1]), dest: matched[2] ?? '' }
}

function nestFolders(kbId: number, folders: KbFolderMark[]): KbTargetNode[] {
  const roots: KbTargetNode[] = []
  const index = new Map<string, KbTargetNode>()
  const ordered = [...folders].sort((a, b) => a.path.localeCompare(b.path, 'zh-CN'))
  for (const folder of ordered) {
    const parts = folder.path.split('/').filter(Boolean)
    let parent = roots
    let acc = ''
    parts.forEach((part, indexInPath) => {
      acc = acc ? `${acc}/${part}` : part
      let node = index.get(acc)
      if (!node) {
        node = {
          title: part,
          value: `kb:${kbId}|${acc}`,
          children: [],
        }
        index.set(acc, node)
        parent.push(node)
      }
      if (indexInPath === parts.length - 1 && folder.mark) {
        node.title = `${part}（${folder.mark}）`
      }
      parent = node.children ?? []
    })
  }
  const prune = (nodes: KbTargetNode[]) => {
    for (const node of nodes) {
      if (node.children?.length) prune(node.children)
      else delete node.children
    }
  }
  prune(roots)
  return roots
}

export function buildKbTargetTree(
  kbs: KbTargetRow[],
  foldersByKbId: Record<number, KbFolderMark[]> = {},
  ownerText: (row: KbTargetRow) => string | null = () => null,
): KbTargetNode[] {
  const company = kbs
    .filter((kb) => !isPersonalCode(kb.code))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN') || a.kb_id - b.kb_id)
  const personal = kbs
    .filter((kb) => isPersonalCode(kb.code))
    .sort((a, b) => (ownerText(a) || a.name).localeCompare(ownerText(b) || b.name, 'zh-CN') || a.kb_id - b.kb_id)

  const nodes: KbTargetNode[] = company.map((kb) => {
    const children = nestFolders(kb.kb_id, foldersByKbId[kb.kb_id] ?? [])
    return {
      title: `${kb.name}${kb.document_count ? `（${kb.document_count} 篇）` : ''}`,
      value: `kb:${kb.kb_id}`,
      ...(children.length ? { children } : {}),
    }
  })
  if (personal.length) {
    nodes.push({
      title: '个人知识库',
      value: 'group:personal',
      selectable: false,
      children: personal.map((kb) => {
        const children = nestFolders(kb.kb_id, foldersByKbId[kb.kb_id] ?? [])
        return {
          title: `${ownerText(kb) || kb.name}${kb.document_count ? `（${kb.document_count} 篇）` : ''}`,
          value: `kb:${kb.kb_id}`,
          ...(children.length ? { children } : {}),
        }
      }),
    })
  }
  return nodes
}
