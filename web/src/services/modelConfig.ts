import { API_ORIGIN } from './api/client.js'
import { useSettings } from './settings.js'

/** 与 BFF /model/config 对应的模型配置（不含 API Key 明文） */
export type ModelConfig = {
  base_url: string
  protocol: ModelProtocol
  model: string
  timeout: number
  has_api_key: boolean
  source: 'env' | 'file'
}

export type ModelProtocol = 'anthropic' | 'openai'

export type ModelProbe = {
  ok: boolean
  models: string[]
  error: string | null
  latency_ms: number
}

const call = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`${API_ORIGIN}${path}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      'x-api-key': useSettings.getState().apiKey ?? '',
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body: unknown = await response.json()
      if (body && typeof body === 'object' && 'detail' in body) {
        const value = (body as { detail?: unknown }).detail
        if (typeof value === 'string' && value) {
          detail = value
        }
      }
    } catch {
      // 非 JSON 错误响应直接使用状态文本
    }
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export const fetchModelConfig = () => call<ModelConfig>('/model/config')

/** api_key 传空字符串表示保持服务端已保存的 Key 不变 */
export const saveModelConfig = (config: {
  base_url: string
  protocol: ModelProtocol
  model: string
  api_key?: string
}) => call<ModelConfig>('/model/config', { method: 'PUT', body: JSON.stringify(config) })

/** 用草稿配置探测连通性并拉取可用模型，不落盘 */
export const probeModel = (config: {
  base_url: string
  protocol: ModelProtocol
  model: string
  api_key?: string
}) => call<ModelProbe>('/model/probe', { method: 'POST', body: JSON.stringify(config) })
