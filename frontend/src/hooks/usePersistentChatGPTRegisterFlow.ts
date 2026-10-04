import { useEffect, useState } from 'react'

import {
  loadChatGPTRegisterFlow,
  registerFlowUsesPhone,
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
    usesPhone: registerFlowUsesPhone(registerFlow),
  }
}
