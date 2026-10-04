import { useEffect, useState } from 'react'

import {
  CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  loadChatGPTRegistrationMode,
  saveChatGPTRegistrationMode,
  type ChatGPTRegistrationMode,
} from '@/lib/chatgptRegistrationMode'

export function usePersistentChatGPTRegistrationMode() {
  const [mode, setMode] = useState<ChatGPTRegistrationMode>(() =>
    loadChatGPTRegistrationMode(),
  )

  useEffect(() => {
    saveChatGPTRegistrationMode(mode)
  }, [mode])

  return {
    mode,
    setMode,
    hasRefreshTokenSolution:
      mode === CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
  }
}
