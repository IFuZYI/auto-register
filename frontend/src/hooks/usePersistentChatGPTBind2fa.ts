import { useEffect, useState } from 'react'

import { loadChatGPTBind2fa, saveChatGPTBind2fa } from '@/lib/chatgptBind2fa'

export function usePersistentChatGPTBind2fa() {
  const [bind2fa, setBind2fa] = useState<boolean>(() => loadChatGPTBind2fa())

  useEffect(() => {
    saveChatGPTBind2fa(bind2fa)
  }, [bind2fa])

  return { bind2fa, setBind2fa }
}
