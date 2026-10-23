import { describe, expect, it } from 'vitest'
import { buildOrgTree, buildUserOrgTree, collectDeptKeys, rootDeptKeys, type DeptNode } from './deptTree'

const ROWS: DeptNode[] = [
  {
    dept_id: 1,
    dingtalk_dept_id: '1',
    name: '重庆熙街',
    parent_dept_id: null,
    members: [{ user_id: 5, dingtalk_userid: 'u5', nick: '雷航', title: '管理员', is_primary: 1 }],
  },
  {
    dept_id: 24,
    dingtalk_dept_id: '97478025',
    name: '物业中心',
    parent_dept_id: 1,
    members: [],
  },
  {
    dept_id: 33,
    dingtalk_dept_id: '97486022',
    name: '工程部',
    parent_dept_id: 24,
    dept_code: '',
    members: [{ user_id: 9, dingtalk_userid: 'u9', nick: '张工', title: '工程师', is_primary: 1 }],
  },
  {
    dept_id: 2,
    dingtalk_dept_id: '102',
    name: '人力资源部',
    parent_dept_id: 1,
    dept_code: 'hr',
    members: [{ user_id: 2, dingtalk_userid: 'alice', nick: 'Alice', title: 'HR 专员', is_primary: 1 }],
  },
]

describe('组织架构树', () => {
  it('按上下级折叠，人员挂在直属部门下', () => {
    const tree = buildOrgTree(ROWS)
    expect(tree.map((branch) => branch.dept.name)).toEqual(['重庆熙街'])
    expect(tree[0].members.map((member) => member.nick)).toEqual(['雷航'])
    const names = tree[0].children.map((branch) => branch.dept.name)
    expect(names).toEqual(['物业中心', '人力资源部'])
    const property = tree[0].children[0]
    expect(property.children[0].dept.name).toBe('工程部')
    expect(property.children[0].members[0].nick).toBe('张工')
    expect(rootDeptKeys(ROWS)).toEqual(['dept-1'])
  })

  it('搜人员时只留命中的人，并展开到该部门', () => {
    const tree = buildOrgTree(ROWS, '张工')
    expect(collectDeptKeys(tree)).toEqual(['dept-1', 'dept-24', 'dept-33'])
    expect(tree[0].members).toEqual([])
    expect(tree[0].children[0].children[0].members.map((member) => member.nick)).toEqual(['张工'])
  })

  it('用户只挂在主部门，没有部门的人单独列出', () => {
    const { branches, unassigned } = buildUserOrgTree(ROWS, [
      { user_id: 9, dept_id: 33, nick: '张工' },
      { user_id: 5, dept_id: 33, nick: '兼任到根但主部门是工程部' },
      { user_id: 365, dept_id: null, nick: '管理员' },
      { user_id: 8, dept_id: 999, nick: '部门已删除' },
    ])
    expect(branches[0].users).toEqual([])
    const engineering = branches[0].children.find((branch) => branch.dept.dept_id === 24)?.children[0]
    expect(engineering?.users.map((user) => user.user_id)).toEqual([5, 9])
    expect(unassigned.map((user) => user.user_id)).toEqual([8, 365])
  })

  it('搜部门码或部门 ID 时保留该部门的全部人员', () => {
    const tree = buildOrgTree(ROWS, 'hr')
    const hr = tree[0].children.find((branch) => branch.dept.dept_id === 2)
    expect(hr?.members.map((member) => member.nick)).toEqual(['Alice'])
    expect(collectDeptKeys(tree)).not.toContain('dept-33')
  })
})
