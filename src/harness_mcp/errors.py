"""业务错误只包含稳定错误码与可公开说明，不携带凭证和原始命令输出。"""


class HarnessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def error_payload(error: Exception) -> dict:
    if hasattr(error, "code") and hasattr(error, "message"):
        return {"code": str(error.code), "message": str(error.message)}
    return {"code": "INTERNAL_ERROR", "message": "本地任务处理失败，请检查脱敏诊断。"}

