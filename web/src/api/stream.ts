/**
 * SSE 流式客户端。
 *
 * 为什么不用 EventSource：它只支持 GET，而流式接口需要携带 JSON body。
 * 因此用 fetch + ReadableStream 手动解析 SSE 帧。
 */
import { clearToken, getToken } from './http'
import type { StreamEvent } from './types'

export interface StreamHandlers {
  onOpen?: () => void
  onToken?: (text: string) => void
  onField?: (key: string, value: unknown) => void
  onStart?: (data: Record<string, any>) => void
  onDone?: (result: Record<string, any>) => void
  onDegraded?: (reason: string) => void
  onError?: (message: string) => void
  onClose?: () => void
}

/** 解析一个 SSE 帧（可能含多行 data） */
function parseFrame(raw: string): StreamEvent | null {
  const lines = raw.split('\n')
  let event = 'message'
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim())
    }
  }
  if (!dataLines.length) return null
  const text = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(text) }
  } catch {
    return { event, data: { raw: text } }
  }
}

/**
 * 发起流式请求。
 * @returns abort 函数，调用后立即断开，服务端会停止产生 token（省额度）
 */
export function streamAgent(
  body: { agent: string; payload: Record<string, unknown>; use_cache?: boolean },
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController()

  const run = async () => {
    try {
      const resp = await fetch('/api/agent/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        },
        body: JSON.stringify({ ...body, use_cache: body.use_cache ?? true }),
        signal: controller.signal,
      })

      if (resp.status === 401) {
        clearToken()
        handlers.onError?.('登录已过期')
        return
      }
      if (!resp.ok) {
        const text = await resp.text().catch(() => '')
        handlers.onError?.(`HTTP ${resp.status} ${text.slice(0, 200)}`)
        return
      }
      if (!resp.body) {
        handlers.onError?.('浏览器不支持流式响应')
        return
      }

      handlers.onOpen?.()

      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE 以空行分隔帧
        let sep = buffer.indexOf('\n\n')
        while (sep >= 0) {
          const frame = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          const evt = parseFrame(frame)
          if (evt) {
            switch (evt.event) {
              case 'start':
                handlers.onStart?.(evt.data)
                break
              case 'token':
                handlers.onToken?.(String(evt.data.text ?? ''))
                break
              case 'field':
                handlers.onField?.(String(evt.data.key ?? ''), evt.data.value)
                break
              case 'degraded':
                handlers.onDegraded?.(String(evt.data.reason ?? ''))
                break
              case 'done':
                handlers.onDone?.(evt.data.result ?? {})
                break
              case 'error':
                handlers.onError?.(String(evt.data.error ?? '未知错误'))
                break
              case 'closed':
                handlers.onClose?.()
                break
            }
          }
          sep = buffer.indexOf('\n\n')
        }
      }
      handlers.onClose?.()
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        handlers.onClose?.()
        return
      }
      handlers.onError?.((e as Error).message)
    }
  }

  void run()
  return () => controller.abort()
}

/** 订阅后台任务的进度事件流（GET SSE） */
export function streamTask(
  taskId: string,
  handlers: {
    onProgress?: (data: { progress: number; stage: string }) => void
    onSummary?: (data: Record<string, any>) => void
    onDone?: (data: Record<string, any>) => void
    onError?: (message: string) => void
    onClose?: () => void
  },
): () => void {
  const url = `/api/agent/stream?task_id=${encodeURIComponent(taskId)}`
  const controller = new AbortController()

  const run = async () => {
    try {
      const resp = await fetch(url, {
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
        signal: controller.signal,
      })
      if (!resp.ok || !resp.body) {
        handlers.onError?.(`HTTP ${resp.status}`)
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let sep = buffer.indexOf('\n\n')
        while (sep >= 0) {
          const evt = parseFrame(buffer.slice(0, sep))
          buffer = buffer.slice(sep + 2)
          if (evt) {
            if (evt.event === 'progress') handlers.onProgress?.(evt.data as any)
            else if (evt.event === 'summary') handlers.onSummary?.(evt.data)
            else if (evt.event === 'done') handlers.onDone?.(evt.data)
            else if (evt.event === 'failed') handlers.onError?.(String(evt.data.error ?? '任务失败'))
            else if (evt.event === 'closed') {
              handlers.onClose?.()
              return
            }
          }
          sep = buffer.indexOf('\n\n')
        }
      }
      handlers.onClose?.()
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      handlers.onError?.((e as Error).message)
    }
  }

  void run()
  return () => controller.abort()
}
