"""浏览器公共实现：统一错误。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from server.python.log_s.client_safe_log import REDACT_CLIENT_LOGS
from server.python.log_s import log, safe_client_text
from server.python.log_s.error_diagnostics import diagnostic_text, diagnostic_value


@dataclass(slots=True)
class BrowserError(RuntimeError):
    """所有指纹浏览器对外使用的稳定错误。

    业务代码只根据 ``code``、``retryable`` 和 ``retry_after`` 决定下一步，
    不解析厂商英文文案。内部使用时 ``details`` 保留厂商原始诊断。
    """

    provider: str
    operation: str
    code: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def diagnostic_message(self) -> str:
        """探针把错误转成状态前，先落盘完整诊断，再给界面有限长度提示。"""
        log.error(str(self), exc_info=(type(self), self, self.__traceback__), module="浏览器", operation=self.operation)
        return safe_client_text(self)

    def __str__(self) -> str:
        status = f", HTTP {self.status_code}" if self.status_code else ""
        message = (
            f"{self.provider}.{self.operation} "
            f"[{self.code}{status}]: {self.message}"
        )
        if not REDACT_CLIENT_LOGS and self.details:
            message += "；详情：" + json.dumps(diagnostic_value(self.details), ensure_ascii=False)
        if not REDACT_CLIENT_LOGS and self.__cause__ is not None and not isinstance(self.__cause__, BrowserError):
            message += f"；原因：{type(self.__cause__).__name__}: {diagnostic_text(self.__cause__)}"
        return message

__all__ = ["BrowserError"]
