/** 把扁平的部门列表收成可折叠的组织树，并按关键字筛部门和人员。 */

export interface DeptMember {
  user_id: number
  dingtalk_userid: string
  nick?: string
  title?: string | null
  clearance?: number
  status?: string
  is_primary?: number
  roles?: string[]
}

export interface DeptNode {
  dept_id: number
  dingtalk_dept_id: string
  name?: string
  parent_dept_id?: number | null
  tree_depth?: number
  dept_code?: string
  members?: DeptMember[]
}

export interface OrgBranch {
  dept: DeptNode
  members: DeptMember[]
  children: OrgBranch[]
}

function memberMatches(member: DeptMember, keyword: string): boolean {
  const haystack = [member.nick, member.title, member.dingtalk_userid]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return haystack.includes(keyword)
}

function deptMatches(dept: DeptNode, keyword: string): boolean {
  return `${dept.name ?? ''} ${dept.dingtalk_dept_id} ${dept.dept_code ?? ''}`
    .toLowerCase()
    .includes(keyword)
}

/** 先序里的部门按 parent 挂成树。关键字命中人员时只留这些人，并带上上级部门。 */
export function buildOrgTree(rows: DeptNode[], keyword = ''): OrgBranch[] {
  const query = keyword.trim().toLowerCase()
  const byId = new Map(rows.map((row) => [row.dept_id, row]))
  const keep = new Set<number>()
  const membersOf = new Map<number, DeptMember[]>()

  for (const row of rows) {
    const all = row.members ?? []
    if (!query) {
      keep.add(row.dept_id)
      membersOf.set(row.dept_id, all)
      continue
    }
    const hitDept = deptMatches(row, query)
    const people = hitDept ? all : all.filter((member) => memberMatches(member, query))
    if (!hitDept && people.length === 0) continue
    membersOf.set(row.dept_id, people)
    let current: DeptNode | undefined = row
    while (current) {
      keep.add(current.dept_id)
      const parentId: number | null | undefined = current.parent_dept_id
      current = parentId != null ? byId.get(parentId) : undefined
    }
  }

  const childrenOf = new Map<number | null, DeptNode[]>()
  for (const row of rows) {
    if (!keep.has(row.dept_id)) continue
    const parentId = row.parent_dept_id
    const key = parentId != null && keep.has(parentId) && byId.has(parentId) ? parentId : null
    const list = childrenOf.get(key) ?? []
    list.push(row)
    childrenOf.set(key, list)
  }

  const walk = (row: DeptNode): OrgBranch => ({
    dept: row,
    members: membersOf.get(row.dept_id) ?? [],
    children: (childrenOf.get(row.dept_id) ?? []).map(walk),
  })
  return (childrenOf.get(null) ?? []).map(walk)
}

export function collectDeptKeys(branches: OrgBranch[]): string[] {
  const keys: string[] = []
  const walk = (branch: OrgBranch) => {
    keys.push(`dept-${branch.dept.dept_id}`)
    branch.children.forEach(walk)
  }
  branches.forEach(walk)
  return keys
}

export interface ManagedUserRef {
  user_id: number
  dept_id?: number | null
  nick?: string | null
  username?: string | null
  title?: string | null
  dingtalk_userid?: string | null
}

export interface UserOrgBranch {
  dept: DeptNode
  users: ManagedUserRef[]
  children: UserOrgBranch[]
}

function userMatches(user: ManagedUserRef, keyword: string): boolean {
  return [user.nick, user.username, user.title, user.dingtalk_userid, String(user.user_id)]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
    .includes(keyword)
}

/**
 * 用户管理用的组织树。每个人只挂在主部门下，空部门也保留。
 * 没有部门的人由调用方放进「未分配部门」。
 */
export function buildUserOrgTree(
  depts: DeptNode[],
  users: ManagedUserRef[],
  keyword = '',
): { branches: UserOrgBranch[]; unassigned: ManagedUserRef[] } {
  const query = keyword.trim().toLowerCase()
  const deptIds = new Set(depts.map((dept) => dept.dept_id))
  const byId = new Map(depts.map((dept) => [dept.dept_id, dept]))
  const placed = new Map<number, ManagedUserRef[]>()
  const unassigned: ManagedUserRef[] = []
  for (const user of users) {
    const deptId = user.dept_id
    if (deptId == null || !deptIds.has(deptId)) unassigned.push(user)
    else {
      const list = placed.get(deptId) ?? []
      list.push(user)
      placed.set(deptId, list)
    }
  }
  for (const list of placed.values()) {
    list.sort((a, b) => (a.nick || '').localeCompare(b.nick || '', 'zh-CN') || a.user_id - b.user_id)
  }
  unassigned.sort((a, b) => (a.nick || '').localeCompare(b.nick || '', 'zh-CN') || a.user_id - b.user_id)

  const keep = new Set<number>()
  const visibleUsers = new Map<number, ManagedUserRef[]>()
  for (const dept of depts) {
    const people = placed.get(dept.dept_id) ?? []
    if (!query) {
      keep.add(dept.dept_id)
      visibleUsers.set(dept.dept_id, people)
      continue
    }
    const hitDept = deptMatches(dept, query)
    const hitPeople = hitDept ? people : people.filter((user) => userMatches(user, query))
    if (!hitDept && hitPeople.length === 0) continue
    visibleUsers.set(dept.dept_id, hitPeople)
    let current: DeptNode | undefined = dept
    while (current) {
      keep.add(current.dept_id)
      const parentId = current.parent_dept_id
      current = parentId != null ? byId.get(parentId) : undefined
    }
  }

  const childrenOf = new Map<number | null, DeptNode[]>()
  for (const dept of depts) {
    if (!keep.has(dept.dept_id)) continue
    const parentId = dept.parent_dept_id
    const key = parentId != null && keep.has(parentId) && byId.has(parentId) ? parentId : null
    const list = childrenOf.get(key) ?? []
    list.push(dept)
    childrenOf.set(key, list)
  }
  for (const list of childrenOf.values()) {
    list.sort((a, b) => (a.name || '').localeCompare(b.name || '', 'zh-CN') || a.dept_id - b.dept_id)
  }

  const walk = (dept: DeptNode): UserOrgBranch => ({
    dept,
    users: visibleUsers.get(dept.dept_id) ?? [],
    children: (childrenOf.get(dept.dept_id) ?? []).map(walk),
  })
  const filteredUnassigned = query ? unassigned.filter((user) => userMatches(user, query)) : unassigned
  return { branches: (childrenOf.get(null) ?? []).map(walk), unassigned: filteredUnassigned }
}

export function collectUserDeptKeys(branches: UserOrgBranch[]): string[] {
  const keys: string[] = []
  const walk = (branch: UserOrgBranch) => {
    keys.push(`dept-${branch.dept.dept_id}`)
    branch.children.forEach(walk)
  }
  branches.forEach(walk)
  return keys
}

export function rootDeptKeys(rows: DeptNode[]): string[] {
  const ids = new Set(rows.map((row) => row.dept_id))
  return rows
    .filter((row) => row.parent_dept_id == null || !ids.has(row.parent_dept_id))
    .map((row) => `dept-${row.dept_id}`)
}
