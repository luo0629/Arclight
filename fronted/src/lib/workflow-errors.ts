export function workflowErrorMessage(code: string) {
  switch (code) {
    case "SERVICE_BALANCE_INSUFFICIENT":
      return "余额不足，请前往对应服务商充值后重试。";
    case "SERVICE_AUTHENTICATION_FAILED":
      return "API Key 无效，请检查模型服务配置。";
    case "SERVICE_RATE_LIMITED":
      return "模型服务请求过于频繁，请稍后重试。";
    case "SERVICE_QUOTA_EXCEEDED":
      return "模型服务额度已达上限，请前往对应服务商检查额度后重试。";
    default:
      return `错误代码：${code}。请检查服务配置和 Worker 日志。`;
  }
}
