"""데스크톱 알림 — 발송 실패·수동 처리 필요·6시간 초과 등 사용자에게 가시화.

Windows: `winotify` 사용 (토스트 알림). 다른 OS 또는 winotify 미설치 환경에선
로그로 fallback.

PRD §FR-3.4, §NFR-4.2 참조.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)

# winotify 는 Windows 전용 — 다른 OS 에선 import 시점에 실패 가능
try:
    if sys.platform == "win32":
        from winotify import Notification  # type: ignore
    else:
        Notification = None  # type: ignore[assignment, misc]
except ImportError:  # pragma: no cover
    Notification = None  # type: ignore[assignment, misc]


class NotificationLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    body: str
    level: NotificationLevel = NotificationLevel.INFO


class Notifier:
    """데스크톱 알림 표시기.

    실제 토스트는 Windows 에서만 동작. 그 외 환경에선 로그로만 출력.
    테스트 친화적이게 `last_notification` 으로 마지막 알림 확인 가능.
    """

    def __init__(self, app_name: str = "Lovable Followup Agent") -> None:
        self._app_name = app_name
        self.last_notification: NotificationMessage | None = None

    def notify(self, message: NotificationMessage) -> None:
        self.last_notification = message
        log_func = {
            NotificationLevel.INFO: log.info,
            NotificationLevel.WARNING: log.warning,
            NotificationLevel.ERROR: log.error,
        }[message.level]
        log_func("Notify[%s] %s — %s", message.level.value, message.title, message.body)

        if Notification is None or sys.platform != "win32":
            return  # Windows 외에선 로그만

        try:
            toast = Notification(
                app_id=self._app_name,
                title=message.title,
                msg=message.body,
                duration="short",
            )
            toast.show()
        except Exception:
            log.exception("데스크톱 토스트 표시 실패 (로그만 남김)")

    # ─── 편의 메서드 ───
    def info(self, title: str, body: str) -> None:
        self.notify(NotificationMessage(title, body, NotificationLevel.INFO))

    def warning(self, title: str, body: str) -> None:
        self.notify(NotificationMessage(title, body, NotificationLevel.WARNING))

    def error(self, title: str, body: str) -> None:
        self.notify(NotificationMessage(title, body, NotificationLevel.ERROR))
