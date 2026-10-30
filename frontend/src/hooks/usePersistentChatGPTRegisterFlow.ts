import { useEffect, useState } from 'react'

import {
  loadChatGPTRegisterFlow,
  saveChatGPTRegisterFlow,
  type ChatGPTRegisterFlow,
} from '@/lib/chatgptRegisterFlow'

export function usePersistentChatGPTRegisterFlow() {
  const [registerFlow, setRegisterFlow] = useState<ChatGPTRegisterFlow>(() =>
    loadChatGPTRegisterFlow(),
  )

  useEffect(() => {
    saveChatGPTRegisterFlow(registerFlow)
  }, [registerFlow])

  return {
    registerFlow,
    setRegisterFlow,
  }
}
