import clsx from 'clsx'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslate } from '../../i18n/index.js'
import { copyText } from '../../utils/clipboard.js'
import { IconCheckOutline16, IconCopyOutline16 } from '../base/icons.js'
import { showToast } from '../base/toastState.js'
import { CodeBlock } from './CodeBlock.js'
import styles from './QueryLog.module.css'

type QueryRecord = {
  n: number
  tool: string
  source: string
  database: string
  ok: boolean
  error: string
  detail: string
  sql: string
  rows: number | null
  cached: boolean
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '')

const parsePayload = (payload: string): QueryRecord[] | null => {
  try {
    const data: unknown = JSON.parse(payload)
    if (!data || typeof data !== 'object') {
      return null
    }
    const list = (data as { records?: unknown }).records
    if (!Array.isArray(list)) {
      return null
    }
    const records: QueryRecord[] = []
    for (const value of list) {
      if (!value || typeof value !== 'object') {
        continue
      }
      const item = value as Record<string, unknown>
      const tool = asString(item.tool)
      if (!tool) {
        continue
      }
      records.push({
        n: typeof item.n === 'number' ? item.n : records.length + 1,
        tool,
        source: asString(item.source),
        database: asString(item.database),
        ok: item.ok !== false,
        error: asString(item.error),
        detail: asString(item.detail),
        sql: asString(item.sql),
        rows: typeof item.rows === 'number' ? item.rows : null,
        cached: item.cached === true,
      })
    }
    return records
  } catch {
    return null
  }
}

const CopyRecordButton = memo(function CopyRecordButton({ text }: { text: string }) {
  const t = useTranslate()
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useEffect(() => () => clearTimeout(timerRef.current), [])

  return (
    <button
      type="button"
      className={styles.copy}
      title={t('copy')}
      onClick={async () => {
        if (!(await copyText(text))) {
          showToast({ key: 'copyFailed' })
          return
        }
        setCopied(true)
        clearTimeout(timerRef.current)
        timerRef.current = setTimeout(() => setCopied(false), 1500)
      }}
    >
      {copied ? <IconCheckOutline16 /> : <IconCopyOutline16 />}
      <span>{t(copied ? 'copied' : 'copy')}</span>
    </button>
  )
})

const RecordCard = memo(function RecordCard({ record }: { record: QueryRecord }) {
  const t = useTranslate()
  const [expanded, setExpanded] = useState(false)
  const text = record.sql || record.detail
  const hasSql = record.sql.length > 0
  const long = text.length > 260 || text.split('\n').length > 4
  const sourceLabel = [record.source, record.database].filter(Boolean).join(' · ')

  return (
    <div className={clsx(styles.card, !record.ok && styles.failedCard)}>
      <div className={styles.header}>
        <span className={styles.index}>#{record.n}</span>
        <code className={styles.tool}>{record.tool}</code>
        {sourceLabel ? <span className={styles.source}>{sourceLabel}</span> : null}
        <span className={record.ok ? styles.ok : styles.failed}>
          <span className={styles.dot} />
          {t(record.ok ? 'queryLogSuccess' : 'queryLogFailed')}
        </span>
        {record.rows !== null ? (
          <span className={styles.meta}>{t('queryLogRows', { count: record.rows })}</span>
        ) : null}
        {record.cached ? <span className={styles.cached}>{t('queryLogCached')}</span> : null}
        <CopyRecordButton text={text} />
      </div>
      <div className={styles.body}>
        {text ? (
          hasSql ? (
            <pre className={clsx(styles.sql, long && !expanded && styles.collapsed)}>{text}</pre>
          ) : (
            <div className={styles.detail}>{text}</div>
          )
        ) : (
          <div className={styles.detail}>—</div>
        )}
        {long ? (
          <button
            type="button"
            className={styles.expand}
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {t(expanded ? 'queryLogCollapse' : 'queryLogExpand')}
          </button>
        ) : null}
        {record.error ? <div className={styles.error}>{record.error}</div> : null}
      </div>
    </div>
  )
})

/** 查询记录：解析后端输出的 query-log JSON，渲染为可逐条复制的卡片。 */
export const QueryLog = memo(function QueryLog({
  payload,
  loading,
}: {
  payload: string
  loading: boolean
}) {
  const t = useTranslate()
  const records = useMemo(() => parsePayload(payload), [payload])

  if (records === null) {
    // 流式输出期间 JSON 还不完整，先留白；结束后仍解析失败则退回代码块，避免丢数据。
    if (loading) {
      return <div className={styles.pending}>{t('queryLogPending')}</div>
    }
    return <CodeBlock code={payload} infostring="json" />
  }
  if (records.length === 0) {
    return <div className={styles.empty}>{t('queryLogEmpty')}</div>
  }
  return (
    <div className={styles.wrap}>
      <div className={styles.toolbar}>{t('queryLogCount', { count: records.length })}</div>
      {records.map((record) => (
        <RecordCard key={record.n} record={record} />
      ))}
    </div>
  )
})
