"""Conversation history and model state per Discord channel."""

import time
from collections import defaultdict

from config import DEFAULT_MODEL, MAX_HISTORY, HISTORY_TTL, SYSTEM_PROMPT


class ConversationStore:
    def __init__(self):
        self._history: dict[int, list[dict]] = defaultdict(list)
        self._models: dict[int, str] = {}
        self._timestamps: dict[int, float] = {}

    def get_model(self, ch: int) -> str:
        return self._models.get(ch, DEFAULT_MODEL)

    def set_model(self, ch: int, model: str):
        self._models[ch] = model

    def add(self, ch: int, msg: dict):
        self._history[ch].append(msg)
        if len(self._history[ch]) > MAX_HISTORY:
            self._history[ch] = self._history[ch][-MAX_HISTORY:]
        self._timestamps[ch] = time.time()

    def get_messages(self, ch: int) -> list[dict]:
        if ch in self._timestamps and time.time() - self._timestamps[ch] > HISTORY_TTL:
            self.clear(ch)
        return [{"role": "system", "content": SYSTEM_PROMPT}] + self._history[ch]

    def clear(self, ch: int):
        self._history.pop(ch, None)
        self._timestamps.pop(ch, None)

    def summary(self, ch: int) -> str:
        return f"model: {self.get_model(ch)} | 履歴: {len(self._history.get(ch, []))}件"
