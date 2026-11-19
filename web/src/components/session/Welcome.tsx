import { useIsMobile } from '../../app/viewport.js'
import { useTranslate } from '../../i18n/index.js'
import { sendMessage } from '../../services/chat/store.js'
import { setDraftText } from '../../services/chat/drafts.js'
import { ChatInput } from '../chatInput/ChatInput.js'
import { MobileHeader } from './MobileHeader.js'
import styles from './Welcome.module.css'

const SUGGESTIONS = [
  '南网能源昨天总用电多少？',
  '荣耀近 30 天用电趋势？',
  '南网能源哪块表用电最多？',
  '南网能源 36 层现在室温多少？',
  '南网能源昨天控制成功率多少？',
  '南网能源 MySQL 里有多少设备？',
  '南网能源配置了哪些冷站？',
  '目前支持哪些租户的数据？',
]

/** 新对话：问候语 + 输入框 + 能效示例问题。桌面端居中；移动端输入框吸底。 */
export const Welcome = () => {
  const t = useTranslate()
  const isMobile = useIsMobile()

  const ask = (question: string) => {
    setDraftText(null, question)
    void sendMessage(null)
  }

  return (
    <div className={styles.container}>
      {isMobile ? <MobileHeader /> : null}
      <div className={styles.welcome}>
        <div className={styles.greeting}>{t('welcomeMessageChat250808')}</div>
        <ChatInput sessionId={null} />
        <div className={styles.suggestions}>
          {SUGGESTIONS.map((question) => (
            <button
              key={question}
              type="button"
              className={styles.suggestion}
              onClick={() => ask(question)}
            >
              {question}
            </button>
          ))}
        </div>
        {isMobile ? <div className={styles.caveat}>{t('contentCaveat')}</div> : null}
      </div>
    </div>
  )
}
