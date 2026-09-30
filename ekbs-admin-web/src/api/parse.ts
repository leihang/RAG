import { http } from './client'
import type { ChunkNode, ChunkTree, ParseTaskList } from './types'

export interface ParseTaskQuery {
  limit?: number
  offset?: number
  status?: string
  kb_id?: number
}

export const listParseTasks = (params: ParseTaskQuery = {}) =>
  http.get('/parse/tasks', { params }) as Promise<ParseTaskList>

/** 任务详情是嵌套的：`{task, state_machine}`（状态机是允许的状态序列）。 */
export const getParseTask = (taskId: number) =>
  http.get(`/parse/tasks/${taskId}`) as Promise<{
    task: Record<string, unknown>
    state_machine?: string[]
  }>

/** 解析结果预览：后端返回的是 `preview`（不是 `markdown`），另有分块结构 `structure`。 */
export const getParseResult = (taskId: number) =>
  http.get(`/parse/tasks/${taskId}/result`) as Promise<{
    task_id: number
    structure?: ChunkNode[]
    chunk_count?: number
    preview?: string
    preview_truncated?: boolean
    note?: string
  }>

export const retryParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/retry`) as Promise<unknown>

export const cancelParseTask = (taskId: number) =>
  http.post(`/parse/tasks/${taskId}/cancel`) as Promise<unknown>

export const getParseConfig = () =>
  http.get('/parse/config') as Promise<Record<string, unknown>>

export const getChunkTree = (docId: number) =>
  http.get(`/chunks/documents/${docId}/tree`) as Promise<ChunkTree>

export const getChunk = (chunkId: number) =>
  http.get(`/chunks/${chunkId}`) as Promise<Record<string, unknown>>

export const updateChunk = (chunkId: number, body: { content: string; reason?: string }) =>
  http.put(`/chunks/${chunkId}`, body) as Promise<unknown>

export const splitChunk = (chunkId: number, body: { offset?: number; reason?: string } = {}) =>
  http.post(`/chunks/${chunkId}/split`, body) as Promise<unknown>

export const mergeChunk = (chunkId: number, body: { reason?: string } = {}) =>
  http.post(`/chunks/${chunkId}/merge`, body) as Promise<unknown>

export const reembedChunk = (chunkId: number) =>
  http.post(`/chunks/${chunkId}/reembed`) as Promise<unknown>

export const revectorizeDocument = (docId: number) =>
  http.post(`/chunks/documents/${docId}/revectorize`) as Promise<unknown>
