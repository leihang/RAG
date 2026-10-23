import { describe, expect, it } from 'vitest'
import { buildBreadcrumb, filterNav, NAV } from './nav'

const flat = (items: ReturnType<typeof filterNav>): string[] =>
  items.flatMap((i) => (i.children ? i.children.map((c) => c.name) : [i.name]))

describe('菜单按权限过滤（处理流程侧栏）', () => {
  it('无任何权限点时菜单为空（不渲染禁用项）', () => {
    expect(filterNav(NAV, () => false)).toEqual([])
  })

  it('只有读权限时看不到上传入口，但能看文档管理', () => {
    const has = (c: string) => c === 'doc:document:read'
    expect(flat(filterNav(NAV, has))).toEqual(['文档管理', '回收站'])
  })

  it('父节点在没有任何可见子节点时被隐藏', () => {
    const has = (c: string) => c === 'sys:dashboard:read'
    const tree = filterNav(NAV, has)
    expect(tree.map((n) => n.name)).toEqual(['概览看板'])
    expect(tree[0].children).toBeUndefined()
  })

  it('auditor 视角只有审计三项（且不含任何写入口）', () => {
    const auditPerms = new Set(['audit:qa:read', 'audit:qa:read_all', 'audit:trace:read'])
    const names = flat(filterNav(NAV, (c) => auditPerms.has(c)))
    expect(names.sort()).toEqual(['安全事件', '数据访问日志', '问答日志'].sort())
    expect(names).not.toContain('上传文档')
  })

  it('全部权限时处理流程四项 + 资产两项 + 其它模块都在；侧栏无目录导入', () => {
    const all = new Set(
      NAV.flatMap((i) => (i.children?.length ? i.children.map((c) => c.perm!) : [i.perm!])),
    )
    const names = flat(filterNav(NAV, (c) => all.has(c)))
    // 概览 + 处理流程4 + 知识资产2 + iam5 + logs3 = 15
    expect(names).toHaveLength(15)
    expect(names).toContain('上传文档')
    expect(names).not.toContain('目录导入')
    expect(names).toContain('清洗解析')
    expect(names).toContain('分块预览')
    expect(names).toContain('向量检索')
  })

  it('写权限时侧栏只有上传文档（目录导入改从上传页进入）', () => {
    const writer = flat(filterNav(NAV, (c) => c === 'doc:document:create'))
    expect(writer).toEqual(['上传文档'])
  })

  it('处理流程分组名称正确', () => {
    expect(NAV.find((n) => n.path === '/pipeline')?.name).toBe('处理流程')
  })
})

describe('面包屑（方案 05 §2.2）', () => {
  it('二级页面显示「一级 / 二级」', () => {
    expect(buildBreadcrumb('/docs/list')).toEqual(['知识资产', '文档管理'])
    expect(buildBreadcrumb('/docs/trash')).toEqual(['知识资产', '回收站'])
    expect(buildBreadcrumb('/docs/upload')).toEqual(['处理流程', '上传文档'])
    expect(buildBreadcrumb('/docs/import')).toEqual(['处理流程', '上传文档'])
    expect(buildBreadcrumb('/parse/tasks')).toEqual(['处理流程', '清洗解析'])
    expect(buildBreadcrumb('/chunks')).toEqual(['处理流程', '分块预览'])
    expect(buildBreadcrumb('/rag/playground')).toEqual(['处理流程', '向量检索'])
    expect(buildBreadcrumb('/iam/accounts')).toEqual(['权限与安全', '管理台账号'])
    expect(buildBreadcrumb('/iam/depts')).toEqual(['权限与安全', '组织架构'])
  })

  it('详情页归到所属二级菜单', () => {
    expect(buildBreadcrumb('/docs/42')).toEqual(['知识资产', '文档管理'])
    expect(buildBreadcrumb('/chunks/42')).toEqual(['处理流程', '分块预览'])
    expect(buildBreadcrumb('/parse/review/76')).toEqual(['处理流程', '清洗解析'])
  })

  it('一级页面只显示一级', () => {
    expect(buildBreadcrumb('/dashboard')).toEqual(['概览看板'])
  })

  it('未知路径退化为「管理台」，不抛异常', () => {
    expect(buildBreadcrumb('/nope/deep/path')).toEqual(['管理台'])
  })
})
