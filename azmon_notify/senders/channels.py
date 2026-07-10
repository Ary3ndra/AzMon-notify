"""Concrete channels: Windows toast, ntfy (phone push), Telegram.

The web dashboard is itself a 'channel' (in-page banner + sound + browser
notification) handled in the web layer — these are for native / off-screen push.
"""
from __future__ import annotations

import logging

import requests

from .base import Notifier, register

log = logging.getLogger("azmon.send")


@register("windows_toast")
class WindowsToast(Notifier):
    """Native Windows 11 toast via winotify. Sev0/1 get an alarm sound."""

    def __init__(self, conf: dict):
        super().__init__(conf)
        try:
            from winotify import Notification, audio  # noqa: F401
        except Exception as ex:  # noqa: BLE001
            raise RuntimeError(
                "winotify not installed (pip install winotify) or not on Windows"
            ) from ex
        self._N = Notification
        self._audio = audio
        self.app_id = conf.get("app_id", "Azure Monitor")

    def send_group(self, group_key: str, alerts: list) -> None:
        t = self._N(app_id=self.app_id,
                    title=self.title(group_key, alerts),
                    msg=self.body(alerts),
                    duration="long" if self.worst_severity(alerts) <= 1 else "short")
        snd = self._audio.LoopingAlarm if self.worst_severity(alerts) <= 1 \
            else self._audio.Default
        try:
            t.set_audio(snd, loop=False)
        except Exception:  # noqa: BLE001
            pass
        t.show()


@register("ntfy")
class NtfyNotifier(Notifier):
    """Phone push. Use ntfy.sh (free) or self-host. Install the ntfy app,
    subscribe to your topic, done — alerts on your phone over any network."""

    def __init__(self, conf: dict):
        super().__init__(conf)
        self.url = f"{conf['server'].rstrip('/')}/{conf['topic']}"
        self.headers = {}
        if conf.get("token"):
            self.headers["Authorization"] = f"Bearer {conf['token']}"

    def send_group(self, group_key: str, alerts: list) -> None:
        sev = self.worst_severity(alerts)
        prio = {0: "5", 1: "4", 2: "3", 3: "2", 4: "1"}.get(sev, "3")
        tags = {0: "rotating_light", 1: "x", 2: "warning"}.get(sev, "bell")
        # HTTP headers are latin-1 only; resource/rule names can be unicode.
        title = self.title(group_key, alerts).encode("latin-1", "replace").decode("latin-1")
        h = dict(self.headers, Title=title, Priority=prio, Tags=tags)
        try:
            requests.post(self.url, data=self.body(alerts).encode(),
                          headers=h, timeout=15).raise_for_status()
        except Exception as ex:  # noqa: BLE001
            log.error("ntfy send failed: %s", ex)


@register("telegram")
class TelegramNotifier(Notifier):
    def __init__(self, conf: dict):
        super().__init__(conf)
        self.api = f"https://api.telegram.org/bot{conf['bot_token']}/sendMessage"
        self.chat_id = conf["chat_id"]

    def send_group(self, group_key: str, alerts: list) -> None:
        text = f"*{self.title(group_key, alerts)}*\n{self.body(alerts)}"
        try:
            requests.post(self.api, json={
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
            }, timeout=15).raise_for_status()
        except Exception as ex:  # noqa: BLE001
            log.error("telegram send failed: %s", ex)

@register("whatsapp")
class WhatsAppNotifier(Notifier):
    """WhatsApp via CallMeBot (free personal API — no Meta app needed).
    Setup: WhatsApp 'I allow callmebot to send me messages' to +34 644 51 95 23,
    it replies with your apikey. Put your phone (with country code, no +) and
    apikey in config."""

    def __init__(self, conf: dict):
        super().__init__(conf)
        self.phone = str(conf["phone"]).lstrip("+")
        self.apikey = str(conf["apikey"])

    def send_group(self, group_key: str, alerts: list) -> None:
        import urllib.parse
        text = f"{self.title(group_key, alerts)}\n{self.body(alerts)}"
        url = ("https://api.callmebot.com/whatsapp.php?"
               f"phone={self.phone}&apikey={self.apikey}"
               f"&text={urllib.parse.quote(text)}")
        try:
            requests.get(url, timeout=20).raise_for_status()
        except Exception as ex:  # noqa: BLE001
            log.error("whatsapp send failed: %s", ex)

# Future hooks: quiet-hours / per-severity routing wrappers, generic webhook,
# Teams-via-Graph if you ever get an app registration.
