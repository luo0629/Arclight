const messages: Readonly<Record<string, string>> = {
  GATEWAY_QUOTA_EXHAUSTED:
    "模型供应商余额不足或额度已耗尽。请到供应商平台充值或调整额度，完成后可重试任务。",
  GATEWAY_PROVIDER_RATE_LIMITED: "模型供应商请求过于频繁，请稍后重试任务。",
  GATEWAY_PROVIDER_AUTHENTICATION_FAILED:
    "模型供应商密钥无效或已失效，请在模型服务设置中更新密钥后重试。",
  GATEWAY_PROVIDER_ACCESS_DENIED:
    "模型供应商拒绝访问，请检查所选模型的使用权限和服务配置。",
  GATEWAY_RECONCILIATION_REQUIRED:
    "模型调用结果尚未确认，请先核验原调用结果后再继续。",
  BUDGET_UNPRICED_HISTORY: "已有金额未知的文字调用，请先核账再启用预算限制。",
};

export function runErrorMessage(code: string): string {
  return messages[code] ?? `错误代码：${code}。请检查服务配置和执行记录。`;
}
