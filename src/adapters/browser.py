"""Shared sync-Playwright browser launch for the site adapters (§7).

One place for browser hygiene so CIAN and Avito behave identically. Honors the
config knobs: proxy, media blocking, and — for hostile anti-bot like Avito —
running a *real* browser (``headless=False``, ``channel='chrome'``) with a
persistent user-data dir so cookies from a hand-solved challenge survive. This
is fingerprint hygiene / presenting as an ordinary browser, NOT captcha-solving
or firewall-defeating in code (§7).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _maybe_block(route: Any) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
    else:
        route.continue_()


def launch_context(
    settings: Any, user_agent: str, *, headless_override: bool | None = None
) -> tuple[Any, Any, Any]:
    """Start Playwright and return ``(pw, browser, context)``.

    ``browser`` is ``None`` when a persistent user-data dir is configured (the
    context owns the browser in that mode). ru-RU locale, a normal viewport, and
    a stable UA are always set; media is blocked when ``block_media`` is on.

    ``headless_override`` lets a non-hostile source (CIAN) run headless even when
    ``BROWSER_HEADLESS=0`` is set for the headful Avito grab/warmup.
    """
    from playwright.sync_api import sync_playwright

    headless = settings.browser_headless if headless_override is None else headless_override
    pw = sync_playwright().start()
    launch_kwargs: dict[str, Any] = {"headless": headless}
    if settings.browser_channel:
        launch_kwargs["channel"] = settings.browser_channel
    if settings.proxy_url:
        launch_kwargs["proxy"] = {"server": settings.proxy_url}

    context_kwargs: dict[str, Any] = {
        "locale": "ru-RU",
        "user_agent": user_agent,
        "viewport": {"width": 1366, "height": 900},
    }

    if settings.browser_user_data_dir:
        # Persistent profile: cookies from a manually solved challenge persist,
        # so later runs present as the same already-trusted browser.
        context = pw.chromium.launch_persistent_context(
            settings.browser_user_data_dir, **launch_kwargs, **context_kwargs
        )
        browser = None
        logger.info("launched persistent browser context (headless=%s, channel=%s)",
                    headless, settings.browser_channel or "chromium")
    else:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(**context_kwargs)

    if settings.block_media:
        context.route("**/*", _maybe_block)
    return pw, browser, context


def close_context(pw: Any, browser: Any, context: Any) -> None:
    """Best-effort teardown of what :func:`launch_context` returned."""
    for obj in (context, browser):
        try:
            if obj is not None:
                obj.close()
        except Exception:
            logger.debug("error closing browser object", exc_info=True)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            logger.debug("error stopping playwright", exc_info=True)
