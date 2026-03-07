"""
Keep-alive module for Koyeb/Render/Heroku.
Uses a dedicated daemon thread (not asyncio) so it never stops even if
the Discord event-loop is busy or blocked.
KOYEB_PUBLIC_DOMAIN is set automatically by Koyeb — no manual config needed.
"""
import os
import time
import logging
import threading
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


def _get_app_url() -> str:
    """Detect the public URL from cloud platform environment variables."""
    # Koyeb — set automatically, no user action needed
    if os.environ.get("KOYEB_PUBLIC_DOMAIN"):
        domain = os.environ["KOYEB_PUBLIC_DOMAIN"]
        url = f"https://{domain}" if not domain.startswith("http") else domain
        logger.info(f"[keep-alive] Detected Koyeb URL: {url}")
        return url

    if os.environ.get("KOYEB_SERVICE_NAME"):
        service = os.environ["KOYEB_SERVICE_NAME"]
        url = f"https://{service}.koyeb.app"
        logger.info(f"[keep-alive] Inferred Koyeb URL: {url}")
        return url

    # Manually configured fallback
    if os.environ.get("SERVICE_URL"):
        return os.environ["SERVICE_URL"].rstrip("/")

    # Render
    if os.environ.get("RENDER_EXTERNAL_URL"):
        return os.environ["RENDER_EXTERNAL_URL"].rstrip("/")

    # Heroku
    if os.environ.get("HEROKU_APP_NAME"):
        return f"https://{os.environ['HEROKU_APP_NAME']}.herokuapp.com"

    # Last resort: ping localhost
    port = os.environ.get("PORT", "8000")
    logger.warning(f"[keep-alive] No cloud URL found. Falling back to localhost:{port}")
    return f"http://localhost:{port}"


class KeepAlive:
    """Periodically pings the app's own URL to prevent cloud platforms from sleeping it."""

    def __init__(self, ping_interval_minutes: int = 5):
        self.url = _get_app_url()
        self.interval = ping_interval_minutes * 60
        self._running = False
        self._thread: threading.Thread | None = None
        self.stats = {
            "total": 0,
            "success": 0,
            "failure": 0,
            "last_success": None,
            "last_failure": None,
        }

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="keep-alive")
        self._thread.start()
        logger.info(f"[keep-alive] Started — pinging {self.url} every {self.interval // 60} min")

    def stop(self):
        self._running = False

    def get_stats(self) -> dict:
        total = self.stats["total"]
        rate = round(self.stats["success"] / total * 100, 1) if total else 0
        return {**self.stats, "success_rate": rate, "url": self.url}

    def _loop(self):
        # Wait a bit for the HTTP server to start
        time.sleep(15)
        while self._running:
            self._ping()
            time.sleep(self.interval)

    def _ping(self):
        self.stats["total"] += 1
        for endpoint in ("/health", "/", ""):
            target = self.url.rstrip("/") + endpoint
            try:
                start = time.time()
                resp = requests.get(
                    target,
                    timeout=30,
                    headers={"User-Agent": "KeepAlive/1.0"},
                )
                elapsed = time.time() - start
                if resp.status_code < 500:
                    self.stats["success"] += 1
                    self.stats["last_success"] = datetime.now().isoformat()
                    logger.info(
                        f"[keep-alive] ✅ {target} → {resp.status_code} ({elapsed:.2f}s) "
                        f"[{self.stats['success']}/{self.stats['total']}]"
                    )
                    return  # success — no need to try other endpoints
                logger.warning(f"[keep-alive] ⚠️  {target} → {resp.status_code}")
            except requests.exceptions.Timeout:
                logger.warning(f"[keep-alive] ⏰ Timeout: {target}")
            except requests.exceptions.ConnectionError:
                logger.warning(f"[keep-alive] 🔌 ConnectionError: {target}")
            except Exception as e:
                logger.error(f"[keep-alive] ❌ {target}: {e}")

        # All endpoints failed
        self.stats["failure"] += 1
        self.stats["last_failure"] = datetime.now().isoformat()
        logger.error("[keep-alive] ❌ All endpoints failed. Will retry next cycle.")


# Module-level singleton
_instance: KeepAlive | None = None


def start_keep_alive(ping_interval_minutes: int = 5) -> KeepAlive:
    global _instance
    if _instance and _instance._running:
        return _instance
    _instance = KeepAlive(ping_interval_minutes)
    _instance.start()
    return _instance


def get_stats() -> dict:
    return _instance.get_stats() if _instance else {}
