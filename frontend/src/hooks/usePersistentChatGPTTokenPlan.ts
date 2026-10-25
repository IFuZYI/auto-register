import { useEffect, useState } from 'react'

import {
  loadChatGPTTokenPlan,
  saveChatGPTTokenPlan,
  type ChatGPTTokenPlan,
} from '@/lib/chatgptTokenPlan'

export function usePersistentChatGPTTokenPlan() {
  const [plan, setPlan] = useState<ChatGPTTokenPlan>(() => loadChatGPTTokenPlan())

  useEffect(() => {
    saveChatGPTTokenPlan(plan)
  }, [plan])

  return { plan, setPlan }
}
