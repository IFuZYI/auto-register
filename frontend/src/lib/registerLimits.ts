/**
 * 注册任务的数量与并发上限 —— 与后端 `api/tasks.py` 的 `MAX_REGISTER_COUNT` /
 * `MAX_REGISTER_CONCURRENCY` 保持一致。
 *
 * 前端加 `max` 是「别让用户填出一个会被 422 拒掉的数」；真正的守门人在后端
 * （浏览器原生校验可以被绕过）。两边都设是因为只靠后端的话，用户填完一长串
 * 数字、提交后才看到一个错误，白填一遍。
 */
export const MAX_REGISTER_COUNT = 10000
export const MAX_REGISTER_CONCURRENCY = 200
