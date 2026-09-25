"""Telegram bot: send reports, ask for order approval, read /stop and /resume."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


@dataclass
class Command:
    text: str
    update_id: int


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.http = session or requests.Session()
        self.base = f"https://api.telegram.org/bot{token}"
        self.stop_requested = False
        self._sleep = time.sleep

    def _call(self, method: str, http_timeout: float = 20, attempts: int = 3, **payload):
        # Retry network hiccups (e.g. a keep-alive connection that went stale during a
        # 40-minute analysis). A retried send can very rarely arrive twice; that beats
        # never arriving.
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                r = self.http.post(f"{self.base}/{method}", json=payload,
                                   timeout=http_timeout + 15 * attempt)
                data = r.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last = exc
                log.warning("telegram %s attempt %d failed: %s", method, attempt + 1, exc)
                if attempt + 1 < attempts:
                    self._sleep(2 * (attempt + 1))
        else:
            raise TelegramError(f"{method} failed: {last}") from last
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description')}")
        return data["result"]

    # -- sending -----------------------------------------------------------
    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> int:
        """Send an HTML message. Text longer than Telegram's limit is split."""
        chunks = _split(text, 3900)
        msg_id = 0
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
            msg_id = self._call("sendMessage", **payload)["message_id"]
        return msg_id

    def edit_buttons(self, message_id: int, buttons: list[list[tuple[str, str]]] | None) -> None:
        markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                      for row in (buttons or [])]}
        try:
            self._call("editMessageReplyMarkup", chat_id=self.chat_id,
                       message_id=message_id, reply_markup=markup)
        except TelegramError as exc:  # cosmetic only
            log.debug("edit buttons: %s", exc)

    # -- receiving ---------------------------------------------------------
    def updates(self, offset: int, timeout: int = 0) -> list[dict]:
        return self._call("getUpdates", http_timeout=timeout + 15, offset=offset,
                          allowed_updates=["message", "callback_query"],
                          **({"timeout": timeout} if timeout else {}))

    def _from_me(self, upd: dict) -> bool:
        if "callback_query" in upd:
            chat = upd["callback_query"].get("message", {}).get("chat", {})
        else:
            chat = upd.get("message", {}).get("chat", {})
        return str(chat.get("id")) == self.chat_id

    def read_commands(self, offset: int) -> tuple[list[Command], int]:
        """Return /commands sent since `offset`, and the new offset."""
        cmds = []
        for upd in self.updates(offset):
            offset = max(offset, upd["update_id"] + 1)
            if not self._from_me(upd):
                continue
            text = (upd.get("message", {}).get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(Command(text.split()[0].split("@")[0].lower(), upd["update_id"]))
        return cmds, offset

    def ask_approval(self, plan_id: str, lines: list[str], header: str,
                     offset: int, timeout_minutes: int,
                     sleep=time.sleep, clock=time.monotonic) -> tuple[dict[int, bool], int]:
        """Send each order with Approve / Skip buttons and wait for answers.

        Returns ({order index: approved?}, new offset). Anything not answered
        before the timeout counts as skipped.
        """
        decisions: dict[int, bool] = {}
        msg_ids: dict[int, int] = {}
        self.send(header)
        for i, line in enumerate(lines):
            msg_ids[i] = self.send(
                f"<b>#{i + 1}</b> {html.escape(line)}",
                [[("Approve", f"a:{plan_id}:{i}"), ("Skip", f"s:{plan_id}:{i}")]])
        if len(lines) > 1:
            all_id = self.send("Or decide all at once:",
                               [[("Approve all", f"A:{plan_id}"), ("Skip all", f"S:{plan_id}")]])
        else:
            all_id = None

        deadline = clock() + timeout_minutes * 60
        while len(decisions) < len(lines) and clock() < deadline:
            try:
                ups = self.updates(offset, timeout=25)
            except TelegramError as exc:
                log.warning("telegram poll failed: %s", exc)
                sleep(10)
                continue
            for upd in ups:
                offset = max(offset, upd["update_id"] + 1)
                if not self._from_me(upd):
                    continue
                text = (upd.get("message", {}).get("text") or "").strip().lower()
                if text.startswith("/stop"):
                    self.stop_requested = True  # skip everything still undecided
                    for i in range(len(lines)):
                        decisions.setdefault(i, False)
                    continue
                cq = upd.get("callback_query")
                if not cq:
                    continue
                try:
                    self._call("answerCallbackQuery", callback_query_id=cq["id"])
                except TelegramError:
                    pass
                parts = (cq.get("data") or "").split(":")
                if len(parts) < 2 or parts[1] != plan_id:
                    continue  # a button from an older plan
                kind = parts[0]
                if kind in ("a", "s") and len(parts) == 3 and parts[2].isdigit():
                    i = int(parts[2])
                    if 0 <= i < len(lines) and i not in decisions:
                        decisions[i] = kind == "a"
                        self.edit_buttons(msg_ids[i], [[(
                            "Approved" if kind == "a" else "Skipped", "noop")]])
                elif kind in ("A", "S"):
                    for i in range(len(lines)):
                        if i not in decisions:
                            decisions[i] = kind == "A"
                            self.edit_buttons(msg_ids[i], [[(
                                "Approved" if kind == "A" else "Skipped", "noop")]])
        if all_id:
            self.edit_buttons(all_id, None)
        for i in range(len(lines)):
            if i not in decisions:
                decisions[i] = False
                self.edit_buttons(msg_ids[i], [[("Timed out, skipped", "noop")]])
        return decisions, offset


def _split(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > size and cur:
            parts.append(cur)
            cur = ""
        while len(line) > size:
            parts.append(line[:size])
            line = line[size:]
        cur += line
    if cur:
        parts.append(cur)
    return parts
