const messages: Readonly<Record<string, string>> = {
  ACCOUNT_BALANCE_INSUFFICIENT: "账户余额不足",
  AGENT_MODEL_CALL_LIMIT: "此次自动处理已达到循环次数上限，请查看已保存的执行记录。",
  WORKFLOW_RECOVERY_LIMIT: "自动恢复已达到次数上限，可以手动恢复原操作。",
  ART_CONCEPT_BUDGET:
    "概念图生成将超过启动时冻结的总预算，请查看已有轮次和费用。",
  ART_CONCEPT_INPUT_CHANGED: "概念图能力、配置或输入已变化，请核对后新建任务。",
  ART_CONCEPT_UNRESOLVED: "原概念图结果尚未确认，请先核验原任务。",
  ART_CONCEPT_ROUND_LIMIT: "概念图自动探索已达到轮次上限。",
  SHORT_DRAMA_VISUAL_RETRY_EXHAUSTED: "镜头素材在自动返工次数内未通过审查，请查看镜头任务。",
  ART_CONCEPT_CANDIDATE_LIMIT: "概念图候选总数量已达到启动时冻结的上限。",
  ART_CONCEPT_MEDIA_MISSING:
    "原生成任务缺少可读取的候选图片，请核验媒体导入记录。",
  ART_CONCEPT_REFERENCE_LOCKED: "候选已经锁定，请重新核对精确版本和审核内容。",
  GATEWAY_QUOTA_EXHAUSTED:
    "模型供应商的使用额度已耗尽，请在供应商平台查看额度。",
  GATEWAY_PROVIDER_RATE_LIMITED: "模型供应商请求过于频繁，请稍后重试任务。",
  GATEWAY_PROVIDER_AUTHENTICATION_FAILED:
    "模型供应商密钥无效或已失效，请在模型服务设置中更新密钥后重试。",
  GATEWAY_PROVIDER_ACCESS_DENIED:
    "模型供应商拒绝访问，请检查所选模型的使用权限和服务配置。",
  GATEWAY_RECONCILIATION_REQUIRED:
    "模型调用结果尚未确认，请先核验原调用结果后再继续。",
  BUDGET_UNPRICED_HISTORY: "原任务因费用未对账而被当时的预算规则拦截。",
};

export function runErrorMessage(code: string): string {
  return messages[code] ?? `错误代码：${code}。请检查服务配置和执行记录。`;
}
