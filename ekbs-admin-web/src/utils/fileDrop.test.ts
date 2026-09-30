import { describe, expect, it, vi } from 'vitest'
import { collectFilesFromEntries, relPathOf, type FsEntry } from './fileDrop'

function fakeFile(name: string, path = `/${name}`): File {
  return Object.assign(new File(['x'], name), { __path: path }) as File
}

function fileEntry(name: string, path: string): FsEntry {
  return {
    isFile: true,
    isDirectory: false,
    fullPath: path,
    file: (ok) => ok(fakeFile(name, path)),
  }
}

/** 目录 entry：每次 readEntries 返回一批，最后一批为空（真实浏览器的行为）。 */
function dirEntry(name: string, batches: FsEntry[][], path = `/${name}`): FsEntry {
  return {
    isFile: false,
    isDirectory: true,
    fullPath: path,
    createReader: () => {
      let i = 0
      return {
        readEntries: (ok) => {
          ok(batches[i] ?? [])
          i += 1
        },
      }
    },
  }
}

describe('拖拽/文件夹展开（方案 05 §4.4）', () => {
  it('普通文件直接返回', async () => {
    const out = await collectFilesFromEntries([fileEntry('a.xlsx', '/a.xlsx')])
    expect(out.map((f) => f.name)).toEqual(['a.xlsx'])
  })

  it('文件夹被递归展开成里面的文件', async () => {
    const dir = dirEntry('制度', [[fileEntry('a.xlsx', '/制度/a.xlsx'), fileEntry('b.pdf', '/制度/b.pdf')], []])
    const out = await collectFilesFromEntries([dir])
    expect(out.map((f) => f.name).sort()).toEqual(['a.xlsx', 'b.pdf'])
  })

  it('嵌套子目录也要展开', async () => {
    const sub = dirEntry('2026', [[fileEntry('c.md', '/制度/2026/c.md')], []], '/制度/2026')
    const root = dirEntry('制度', [[sub], []])
    const out = await collectFilesFromEntries([root])
    expect(out.map((f) => f.name)).toEqual(['c.md'])
  })

  it('**必须循环 readEntries 直到空**：一次只给 100 条时不能少传', async () => {
    // 模拟 250 个文件：三次 100/100/50 + 一次空
    const mk = (n: number, from: number) =>
      Array.from({ length: n }, (_, i) => fileEntry(`f${from + i}.md`, `/大目录/f${from + i}.md`))
    const dir = dirEntry('大目录', [mk(100, 0), mk(100, 100), mk(50, 200), []])
    const out = await collectFilesFromEntries([dir])
    expect(out).toHaveLength(250)
    expect(out[249].name).toBe('f249.md')
  })

  it('单个文件读取失败不影响其它文件', async () => {
    const bad: FsEntry = { isFile: true, isDirectory: false, file: (_ok, err) => err?.(new Error('boom')) }
    const out = await collectFilesFromEntries([bad, fileEntry('ok.md', '/ok.md')])
    expect(out.map((f) => f.name)).toEqual(['ok.md'])
  })

  it('相对路径用于区分不同目录下的同名文件', async () => {
    const dir = dirEntry('制度', [[fileEntry('同名.xlsx', '/制度/同名.xlsx')], []])
    const out = await collectFilesFromEntries([dir])
    expect(relPathOf(out[0])).toBe('制度/同名.xlsx')
    // 没有相对路径时回落到文件名
    expect(relPathOf(fakeFile('普通.xlsx'))).toBe('普通.xlsx')
  })

  it('readEntries 永不返回空时**必须终止**（不能挂死）', async () => {
    // 这是实测踩到的：同层 for(;;) 没有计数上限时会无限循环，
    // 深度护栏管不住横向爆炸。靠全局预算把它挡住。
    const self: FsEntry = {
      isFile: false,
      isDirectory: true,
      createReader: () => ({ readEntries: (ok) => ok([self]) }),
    }
    const out = await collectFilesFromEntries([self])
    expect(out).toEqual([])
  })

  it('超过 MAX_FOLDER_FILES 时停止收集', async () => {
    const mk = (n: number, from: number) =>
      Array.from({ length: n }, (_, i) => fileEntry(`f${from + i}.md`, `/大/f${from + i}.md`))
    const dir = dirEntry('大', [mk(100, 0), mk(100, 100), mk(100, 200), mk(100, 300), []])
    const out = await collectFilesFromEntries([dir])
    expect(out.length).toBeLessThanOrEqual(300)
    expect(out.length).toBeGreaterThan(0)
  })
})

describe('dataTransfer 取值', () => {
  it('有 webkitGetAsEntry 时走递归', async () => {
    const { filesFromDataTransfer } = await import('./fileDrop')
    const dt = {
      items: [{ webkitGetAsEntry: () => fileEntry('x.pdf', '/x.pdf') }],
      files: [],
    } as unknown as DataTransfer
    expect((await filesFromDataTransfer(dt)).map((f) => f.name)).toEqual(['x.pdf'])
  })

  it('老浏览器没有 webkitGetAsEntry 时回落到 files', async () => {
    const { filesFromDataTransfer } = await import('./fileDrop')
    const dt = { items: [{}], files: [fakeFile('y.txt')] } as unknown as DataTransfer
    expect((await filesFromDataTransfer(dt)).map((f) => f.name)).toEqual(['y.txt'])
  })

  it('items 为空时也不报错', async () => {
    const { filesFromDataTransfer } = await import('./fileDrop')
    const dt = { items: [], files: [] } as unknown as DataTransfer
    expect(await filesFromDataTransfer(dt)).toEqual([])
  })
})

describe('辅助', () => {
  it('vi 可用（占位，保证测试环境正常）', () => {
    expect(vi.isMockFunction(vi.fn())).toBe(true)
  })
})

describe('上传顺序（按相对路径排序）', () => {
  const mkRel = (rel: string) => {
    const f = new File(['x'], rel.split('/').pop() ?? rel)
    Object.defineProperty(f, 'webkitRelativePath', { value: rel })
    return f
  }

  it('排序结果**确定**：同样的输入永远同样的顺序', async () => {
    const { sortByRelPath, relPathOf } = await import('./fileDrop')
    const shuffled = [mkRel('制度/2026/b.md'), mkRel('制度/a.md'), mkRel('制度/2026/a.md'), mkRel('x.md')]
    const first = sortByRelPath(shuffled).map(relPathOf)
    const second = sortByRelPath([...shuffled].reverse()).map(relPathOf)
    expect(second).toEqual(first)
  })

  it('同一目录下的文件必须相邻（不能交错）', async () => {
    const { sortByRelPath, relPathOf } = await import('./fileDrop')
    const shuffled = [
      mkRel('制度/2026/b.md'), mkRel('制度/a.md'), mkRel('制度/2026/a.md'), mkRel('制度/b.md'),
    ]
    const dirs = sortByRelPath(shuffled).map((f) => relPathOf(f).split('/').slice(0, -1).join('/'))
    // 每个目录名只应出现一次连续段
    const seen = new Set<string>()
    let prev = ''
    for (const d of dirs) {
      if (d !== prev) {
        expect(seen.has(d)).toBe(false)
        seen.add(d)
        prev = d
      }
    }
    expect(seen.size).toBe(2)
  })

  it('同一目录内按文件名升序', async () => {
    const { sortByRelPath, relPathOf } = await import('./fileDrop')
    const out = sortByRelPath([mkRel('d/c.md'), mkRel('d/a.md'), mkRel('d/b.md')]).map(relPathOf)
    expect(out).toEqual(['d/a.md', 'd/b.md', 'd/c.md'])
  })

  it('不修改传入数组', async () => {
    const { sortByRelPath } = await import('./fileDrop')
    const arr = [new File(['x'], 'b.md'), new File(['x'], 'a.md')]
    const copy = [...arr]
    sortByRelPath(arr)
    expect(arr).toEqual(copy)
  })
})
