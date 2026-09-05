"""Credit-free live contract probe for the current Google Flow UI.

This script intentionally stops before generation. It validates authentication,
UI controls and exact lower-priority model selection without spending credits.

Usage:
  python scripts/flow-ui-probe.py --cookie-file "C:\path\cookies.json"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flow_story_studio.flow_credentials import parse_cookie_input
from flow_story_studio.flow_ui_contract import (
    DEFAULT_FLOW_VIDEO_MODEL,
    FlowUIContractError,
    choose_model_candidate,
    model_matches_contract,
    normalize_flow_label,
)

FLOW_URL = "https://flow.google.com/"


def _playwright_cookie(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    value = str(item.get("value") or "")
    domain = str(item.get("domain") or "").strip()
    if not name or not domain:
        return None
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": str(item.get("path") or "/"),
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": bool(item.get("secure", False)),
    }
    same_site = str(item.get("sameSite") or "").casefold()
    if same_site in {"strict", "lax", "no_restriction", "none"}:
        cookie["sameSite"] = {
            "strict": "Strict",
            "lax": "Lax",
            "no_restriction": "None",
            "none": "None",
        }[same_site]
    expiration = item.get("expirationDate")
    if isinstance(expiration, (int, float)) and expiration > 0:
        cookie["expires"] = float(expiration)
    return cookie


async def _visible(locator: Any, timeout: int = 250) -> bool:
    try:
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def _has_exact_control(page: Any, values: tuple[str, ...]) -> bool:
    candidates = page.locator(
        '[role="tab"]:visible, [role="radio"]:visible, '
        '[role="option"]:visible, button:visible'
    )
    for text in await candidates.all_inner_texts():
        folded = normalize_flow_label(text)
        if any(folded == normalize_flow_label(value) for value in values):
            return True
    return False


async def _find_model_trigger(page: Any) -> Any | None:
    buttons = page.locator('button:visible[aria-haspopup="menu"]')
    for index in range((await buttons.count()) - 1, -1, -1):
        button = buttons.nth(index)
        try:
            text = (await button.inner_text(timeout=500)).strip()
            aria = (await button.get_attribute("aria-label")) or ""
        except Exception:
            continue
        folded = normalize_flow_label(f"{text} {aria}")
        if any(token in folded.split() for token in ("veo", "omni", "imagen", "nano")):
            return button
    return None


async def _open_model_menu(page: Any) -> Any:
    trigger = await _find_model_trigger(page)
    if trigger is None:
        settings = page.locator('button[aria-label="Settings"]').first
        if await _visible(settings, 500):
            await settings.click(timeout=2000)
            await page.wait_for_timeout(500)
            trigger = await _find_model_trigger(page)
    if trigger is None:
        raise FlowUIContractError("Could not find a visible Flow model selector.")
    await trigger.click(timeout=2000)
    await page.wait_for_timeout(400)
    return trigger


async def _select_lower_priority(page: Any, trigger: Any) -> str:
    options = page.locator(
        '[role="menuitem"]:visible, [role="menuitemradio"]:visible, '
        '[role="option"]:visible, [role="radio"]:visible'
    )
    labels = await options.all_inner_texts()
    if not labels:
        options = page.locator(
            '[role="menu"]:visible button:visible, '
            '[role="listbox"]:visible button:visible'
        )
        labels = await options.all_inner_texts()
    selected = choose_model_candidate(DEFAULT_FLOW_VIDEO_MODEL, labels)
    await options.nth(selected.index).click(timeout=2000)
    await page.wait_for_timeout(400)
    trigger_text = (await trigger.inner_text(timeout=1000)).strip()
    if not model_matches_contract(DEFAULT_FLOW_VIDEO_MODEL, trigger_text):
        raise FlowUIContractError(
            "Flow did not retain Veo 3.1 Lite Lower Priority after selection."
        )
    return trigger_text


async def _detect_variant(page: Any) -> str:
    if await _visible(page.locator('button[aria-label="Settings"]').first):
        return "agent-or-settings"
    tabs = page.locator('[role="tab"]:visible')
    if await tabs.count():
        return "classic-tabs"
    if "media" in urlparse(page.url).path.casefold():
        return "media-library"
    return "unknown"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cookie_path = Path(args.cookie_file).expanduser().resolve()
    text = cookie_path.read_text(encoding="utf-8")
    _, raw = parse_cookie_input(text)
    if not raw:
        raise FlowUIContractError(
            "Live UI probe requires a JSON cookie export with domain metadata."
        )
    cookies = [cookie for item in raw if (cookie := _playwright_cookie(item)) is not None]
    if not cookies:
        raise FlowUIContractError("No browser-compatible cookies were found.")

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(args.settle_ms)

        host = urlparse(page.url).hostname or ""
        authenticated = "accounts.google." not in host
        variant = await _detect_variant(page)
        prompt_editor = page.locator(
            '[contenteditable="true"]:visible, textarea:visible, '
            '[role="textbox"]:visible'
        ).first
        prompt_editor_found = await _visible(prompt_editor, 800)
        aspect_found = await _has_exact_control(page, ("16:9",))
        count_found = await _has_exact_control(page, ("x1", "1x"))
        duration_controls = [
            value for value in ("4s", "6s", "8s") if await _has_exact_control(page, (value,))
        ]

        model_label = ""
        model_ok = False
        model_error = ""
        try:
            trigger = await _open_model_menu(page)
            model_label = await _select_lower_priority(page, trigger)
            model_ok = True
        except FlowUIContractError as exc:
            model_error = str(exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        generate = page.locator(
            'button[aria-label="Start generation"]:visible, '
            'button[aria-label*="generation" i]:visible'
        ).first
        generate_found = await _visible(generate, 500)

        result = {
            "authenticated": authenticated,
            "host": host,
            "ui_variant": variant,
            "prompt_editor_found": prompt_editor_found,
            "aspect_16_9_found": aspect_found,
            "output_count_1_found": count_found,
            "duration_controls_found": duration_controls,
            "required_model": DEFAULT_FLOW_VIDEO_MODEL,
            "model_selected": model_label,
            "model_contract_ok": model_ok,
            "model_error": model_error,
            "generate_control_found": generate_found,
            "generation_clicked": False,
        }
        result["required_contract_ok"] = all(
            (
                authenticated,
                prompt_editor_found,
                model_ok,
                generate_found,
            )
        )

        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if args.screenshot_on_failure and not result["required_contract_ok"]:
            target = Path(args.screenshot_on_failure).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target), full_page=True)

        await context.close()
        await browser.close()
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", required=True)
    parser.add_argument("--url", default=FLOW_URL)
    parser.add_argument("--settle-ms", type=int, default=3500)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--report", default="data/diagnostics/flow-ui-probe.json")
    parser.add_argument("--screenshot-on-failure", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "required_contract_ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "generation_clicked": False,
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["required_contract_ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
