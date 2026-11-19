import { useEffect, useState } from 'react'
import {
  fetchModelConfig,
  probeModel,
  saveModelConfig,
  type ModelProtocol,
} from '../../services/modelConfig.js'
import { useTranslate } from '../../i18n/index.js'
import { Button } from '../base/Button.js'
import { Select } from '../base/Select.js'
import styles from './Settings.module.css'

type Status = { ok: boolean; text: string } | null

const message = (error: unknown) => (error instanceof Error ? error.message : String(error))

/**
 * 模型配置：修改后立即作用到 BFF 的 Agent（不需要重启）。
 * API Key 只上行、不回显；留空表示沿用服务端已保存的值。
 */
export const ModelSetting = () => {
  const t = useTranslate()
  const [baseUrl, setBaseUrl] = useState('')
  const [protocol, setProtocol] = useState<ModelProtocol>('anthropic')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [hasKey, setHasKey] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [status, setStatus] = useState<Status>(null)
  const [busy, setBusy] = useState<'save' | 'probe' | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchModelConfig()
      .then((config) => {
        if (cancelled) {
          return
        }
        setBaseUrl(config.base_url)
        setProtocol(config.protocol)
        setModel(config.model)
        setHasKey(config.has_api_key)
      })
      .catch((error) => {
        if (!cancelled) {
          setStatus({ ok: false, text: t('modelSettingLoadFailed', { message: message(error) }) })
        }
      })
    return () => {
      cancelled = true
    }
  }, [t])

  const draft = () => ({
    base_url: baseUrl.trim(),
    protocol,
    model: model.trim(),
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
  })

  const runProbe = async () => {
    if (busy) {
      return
    }
    setBusy('probe')
    setStatus(null)
    try {
      const result = await probeModel(draft())
      setModels(result.models)
      setStatus(
        result.ok
          ? {
              ok: true,
              text: t('modelSettingProbeOk', {
                count: result.models.length,
                ms: result.latency_ms,
              }),
            }
          : {
              ok: false,
              text: t('modelSettingProbeFailed', { message: result.error ?? '' }),
            }
      )
    } catch (error) {
      setStatus({
        ok: false,
        text: t('modelSettingProbeFailed', { message: message(error) }),
      })
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (busy) {
      return
    }
    setBusy('save')
    setStatus(null)
    try {
      const config = await saveModelConfig(draft())
      setBaseUrl(config.base_url)
      setProtocol(config.protocol)
      setModel(config.model)
      setHasKey(config.has_api_key)
      setApiKey('')
      setStatus({ ok: true, text: t('modelSettingSaved') })
    } catch (error) {
      setStatus({ ok: false, text: t('modelSettingSaveFailed', { message: message(error) }) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className={styles.cell}>
      <div className={styles.modelForm}>
        <label className={styles.modelField}>
          <span className={styles.label}>{t('modelSettingBaseUrl')}</span>
          <input
            className={styles.modelInput}
            value={baseUrl}
            spellCheck={false}
            autoComplete="off"
            placeholder={t('modelSettingBaseUrlPlaceholder')}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </label>

        <div className={styles.modelField}>
          <span className={styles.label}>{t('modelSettingProtocol')}</span>
          <Select
            ariaLabel={t('modelSettingProtocol')}
            className={styles.modelSelect}
            value={protocol}
            options={[
              { value: 'anthropic', label: t('modelSettingProtocolAnthropic') },
              { value: 'openai', label: t('modelSettingProtocolOpenai') },
            ]}
            onChange={setProtocol}
          />
        </div>

        <label className={styles.modelField}>
          <span className={styles.label}>{t('modelSettingModel')}</span>
          <input
            className={styles.modelInput}
            value={model}
            spellCheck={false}
            autoComplete="off"
            list="energy-agent-model-options"
            placeholder={t('modelSettingModelPlaceholder')}
            onChange={(event) => setModel(event.target.value)}
          />
          <datalist id="energy-agent-model-options">
            {models.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
          {models.length ? (
            <span className={styles.modelHint}>
              {t('modelSettingModelHint', { count: models.length })}
            </span>
          ) : null}
        </label>

        <label className={styles.modelField}>
          <span className={styles.label}>{t('modelSettingApiKey')}</span>
          <input
            className={styles.modelInput}
            type="password"
            value={apiKey}
            autoComplete="off"
            placeholder={
              hasKey ? t('modelSettingApiKeySaved') : t('modelSettingApiKeyPlaceholder')
            }
            onChange={(event) => setApiKey(event.target.value)}
          />
        </label>

        <div className={styles.modelActions}>
          <Button size="m" disabled={busy !== null} onClick={() => void runProbe()}>
            {t(busy === 'probe' ? 'modelSettingProbing' : 'modelSettingProbe')}
          </Button>
          <Button
            size="m"
            variant="outlined"
            disabled={busy !== null}
            onClick={() => void save()}
          >
            {t(busy === 'save' ? 'modelSettingSaving' : 'modelSettingSave')}
          </Button>
        </div>

        {status ? (
          <div className={status.ok ? styles.modelStatus : styles.error} role="status">
            {status.text}
          </div>
        ) : null}
      </div>
    </div>
  )
}
