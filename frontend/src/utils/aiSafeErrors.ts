/**
 * R11.6：稳定错误码 → 可操作中文文案（禁止展示原始异常）。
 */

const RETRIEVAL_MESSAGES: Record<string, string> = {
  E_JUDGE_PROVIDER_FAILED: '裁决服务暂时不可用，请重试',
  E_JUDGE_INVALID_JSON: '裁决结果格式异常，请重试',
  E_JUDGE_SCHEMA: '裁决结果不符合约定，请重试',
  E_JUDGE_MISSING_KEY: '裁决结果不完整，请重试',
  E_JUDGE_DUPLICATE_KEY: '裁决结果重复，请重试',
  E_JUDGE_UNKNOWN_KEY: '裁决结果含未知来源，请重试',
  E_JUDGE_FOUND_INVALID: '裁决结果无效，请重试',
  E_JUDGE_FOUND_INCONSISTENT: '裁决结果不一致，请重试',
  E_JUDGE_COVERAGE: '裁决覆盖不完整，请重试',
  LOOKUP_CANDIDATE_BUILD_FAILED: '候选构建失败，请稍后重试',
  LOOKUP_ANSWER_FAILED: '答案生成失败，请重试',
  LOOKUP_GROUNDING_FAILED: '引用校验失败，请重试',
};

const ORGANIZE_MESSAGES: Record<string, string> = {
  ORGANIZE_PROVIDER_FAILED: '整理服务暂时不可用，请稍后重试',
  JSON_SYNTAX: '整理结果格式异常，请重试',
  SCHEMA_INVALID: '整理结果不符合约定，请重试',
  SOURCE_INVALID: '整理结果引用了无效来源，请重试',
  EMPTY_RESULT: '当前范围没有可保存的整理建议',
  SCOPE_STALE: '整理范围已变化，请重新生成预览',
  ORGANIZE_SCOPE_EMPTY: '当前范围没有记录，请调整筛选后重试',
  ORGANIZE_ENTRY_FORBIDDEN: '包含无权访问的记录',
  ORGANIZE_DAYS_INVALID: '时间范围无效，请重新选择',
  ORGANIZE_ITEM_ID_REQUIRED: '缺少预览项标识，请重新生成预览',
  ORGANIZE_JSON_INVALID: '整理结果格式异常，请重试',
  INTERNAL_ERROR: '整理过程出错，请稍后重试',
};

export function retrievalErrorMessage(code?: string | null, fallback?: string): string {
  if (code && RETRIEVAL_MESSAGES[code]) return RETRIEVAL_MESSAGES[code];
  if (fallback && !looksLikeRawException(fallback)) return fallback;
  return '检索暂时不可用，请重试';
}

export function organizeErrorMessage(code?: string | null, fallback?: string): string {
  if (code && ORGANIZE_MESSAGES[code]) return ORGANIZE_MESSAGES[code];
  if (fallback && !looksLikeRawException(fallback)) return fallback;
  return '整理失败，请稍后重试';
}

export function aiErrorMessage(code?: string | null, fallback?: string): string {
  if (code && (RETRIEVAL_MESSAGES[code] || ORGANIZE_MESSAGES[code])) {
    return RETRIEVAL_MESSAGES[code] || ORGANIZE_MESSAGES[code];
  }
  if (fallback && !looksLikeRawException(fallback)) return fallback;
  return 'AI 服务暂时不可用，请稍后重试';
}

function looksLikeRawException(text: string): boolean {
  return /Traceback|Exception|Error:|at\s+\S+\(|openai|anthropic|api[_\s-]?key|stack/i.test(text);
}
