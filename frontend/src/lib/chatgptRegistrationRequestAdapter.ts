import { DEFAULT_CHATGPT_BIND_2FA } from '@/lib/chatgptBind2fa'
import {
  DEFAULT_TOKEN_PLAN,
  TOKEN_PLAN_ACCESS_ONLY,
  tokenPlanExtra,
  type ChatGPTTokenPlan,
} from '@/lib/chatgptTokenPlan'
import {
  DEFAULT_CHATGPT_REGISTER_FLOW,
  normalizeChatGPTRegisterFlow,
  type ChatGPTRegisterFlow,
} from '@/lib/chatgptRegisterFlow'

type RegistrationExtra = Record<string, unknown>

export interface ChatGPTRegistrationRequestAdapter {
  /** 三选一的「Token 方案」。后端收的两个键由它映射（见 chatgptTokenPlan.ts）。 */
  readonly tokenPlan: ChatGPTTokenPlan
  readonly registerFlow: ChatGPTRegisterFlow
  readonly bind2fa: boolean
  extendExtra(extra: RegistrationExtra): RegistrationExtra
}

abstract class BaseChatGPTRegistrationRequestAdapter
  implements ChatGPTRegistrationRequestAdapter
{
  abstract readonly tokenPlan: ChatGPTTokenPlan
  readonly registerFlow: ChatGPTRegisterFlow
  readonly bind2fa: boolean

  constructor(registerFlow: ChatGPTRegisterFlow, bind2fa: boolean) {
    this.registerFlow = registerFlow
    this.bind2fa = bind2fa
  }

  extendExtra(extra: RegistrationExtra): RegistrationExtra {
    // 方案决定两个键的取值；`hasRefreshTokenSolution` 由方案映射给出，
    // 不再单独维护，避免「mode 说无 RT、兼容键说有 RT」这类自相矛盾。
    return {
      ...extra,
      ...tokenPlanExtra(this.tokenPlan),
      chatgpt_register_flow: this.registerFlow,
      chatgpt_bind_2fa: this.bind2fa,
    }
  }
}

class RefreshTokenChatGPTRegistrationRequestAdapter extends BaseChatGPTRegistrationRequestAdapter {
  readonly tokenPlan: ChatGPTTokenPlan

  constructor(
    registerFlow: ChatGPTRegisterFlow,
    bind2fa: boolean,
    tokenPlan: ChatGPTTokenPlan,
  ) {
    super(registerFlow, bind2fa)
    this.tokenPlan = tokenPlan
  }
}

class AccessTokenOnlyChatGPTRegistrationRequestAdapter extends BaseChatGPTRegistrationRequestAdapter {
  readonly tokenPlan = TOKEN_PLAN_ACCESS_ONLY
}

export function buildChatGPTRegistrationRequestAdapter(
  platform: string | undefined,
  plan: ChatGPTTokenPlan = DEFAULT_TOKEN_PLAN,
  registerFlow: ChatGPTRegisterFlow = DEFAULT_CHATGPT_REGISTER_FLOW,
  bind2fa: boolean = DEFAULT_CHATGPT_BIND_2FA,
): ChatGPTRegistrationRequestAdapter | null {
  if (platform !== 'chatgpt') return null

  const flow = normalizeChatGPTRegisterFlow(registerFlow)
  // 方案是唯一输入：`mode` 与兼容键都从它推导，不再各自维护 ——
  // 否则会出现「方案说无 RT、mode 说要有 RT」这种自相矛盾。
  if (plan === TOKEN_PLAN_ACCESS_ONLY) {
    return new AccessTokenOnlyChatGPTRegistrationRequestAdapter(flow, bind2fa)
  }

  return new RefreshTokenChatGPTRegistrationRequestAdapter(flow, bind2fa, plan)
}
