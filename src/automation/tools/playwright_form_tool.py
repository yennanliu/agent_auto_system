from pathlib import Path
from typing import Literal

from crewai.tools import BaseTool
from playwright.sync_api import Page, sync_playwright
from pydantic import BaseModel

from src.automation import net_guard

FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSc0E2-jTMy8WNFLlHc5rG4zw3U1QaCykBra3mdqFv0DNb8i9Q/viewform"
SCREENSHOT_DIR = Path("data/screenshots")


class FormInput(BaseModel):
    url: str = FORM_URL
    company_name: str
    company_size: Literal["0-10", "11-100", "200 up", "其他"]
    ai_problem: str


class PlaywrightFormTool(BaseTool):
    name: str = "playwright_form_tool"
    description: str = (
        "Fill and submit the AI Consultant Google Form. "
        "Args: url, company_name, company_size (0-10 | 11-100 | 200 up | 其他), ai_problem."
    )
    args_schema: type[BaseModel] = FormInput

    def _run(
        self,
        url: str = FORM_URL,
        company_name: str = "",
        company_size: str = "0-10",
        ai_problem: str = "",
    ) -> dict:
        # `url` is a tool argument (payload / agent), so vet it before a browser
        # is even launched. Chromium can't be pinned to the checked address — the
        # request interceptor + post-navigation check below narrow that gap.
        reason = net_guard.reject_reason(url)
        if reason:
            return {"submitted": False, "error": f"Blocked by SSRF guard: {reason}"}

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            net_guard.guard_page(page)

            try:
                # 'load' fires once HTML + blocking resources are done.
                # Google Forms never reaches 'networkidle' (analytics keep polling).
                net_guard.safe_goto(page, url, wait_until="load", timeout=30_000)

                # Wait until the first real question input appears.
                page.wait_for_selector('input[type="text"]', timeout=15_000)

                result = _fill_and_submit(page, company_name, company_size, ai_problem)
            except net_guard.BlockedURLError as exc:
                browser.close()
                return {"submitted": False, "error": f"Blocked by SSRF guard: {exc}"}
            except Exception as exc:
                page.screenshot(path=str(SCREENSHOT_DIR / "error.png"))
                browser.close()
                raise RuntimeError(f"Form automation failed: {exc}") from exc

            browser.close()

        return result


def _fill_and_submit(page: Page, company_name: str, company_size: str, ai_problem: str) -> dict:
    # ── Q1: 公司名稱 (Company Name) ────────────────────────────────────────────
    page.locator('input[type="text"]').nth(0).fill(company_name)

    # ── Q2: 公司規模 (Company Size) — radio button ────────────────────────────
    # Try progressively broader selectors until one clicks.
    _click_radio(page, company_size)

    # ── Q3: 想用AI解決的問題 (AI Problem) ──────────────────────────────────────
    # Google Forms short-answer = input[type="text"], long-answer = textarea.
    text_inputs = page.locator('input[type="text"]').all()
    if len(text_inputs) >= 2:
        page.locator('input[type="text"]').nth(1).fill(ai_problem)
    else:
        page.locator("textarea").first.fill(ai_problem)

    # ── Submit ──────────────────────────────────────────────────────────────────
    # Google Forms renders the submit button as: <div role="button"><span>提交</span></div>
    # We use a CSS :has() or span approach to avoid matching other role="button" elements.
    submitted = False
    for selector in [
        'div[role="button"]:has(span:text("提交"))',
        'div[role="button"]:has(span:text("Submit"))',
        'div[role="button"]:has-text("提交")',
        'div[role="button"]:has-text("Submit")',
    ]:
        btn = page.locator(selector)
        if btn.count() > 0:
            btn.first.click()
            submitted = True
            break

    if not submitted:
        page.screenshot(path=str(SCREENSHOT_DIR / "no_submit_btn.png"))
        raise RuntimeError("Could not locate submit button")

    # Wait for the confirmation page to load.
    page.wait_for_load_state("load", timeout=15_000)

    # ── Confirmation ────────────────────────────────────────────────────────────
    confirmation = _read_confirmation(page)
    page.screenshot(path=str(SCREENSHOT_DIR / "success.png"))

    return {"submitted": True, "confirmation_text": confirmation}


def _click_radio(page: Page, company_size: str) -> None:
    strategies = [
        lambda: page.get_by_role("radio", name=company_size).first.click(timeout=5_000),
        lambda: page.locator(f'[role="radio"]:has-text("{company_size}")').first.click(timeout=5_000),
        lambda: page.locator(f'label:has-text("{company_size}")').first.click(timeout=5_000),
        lambda: page.get_by_text(company_size, exact=True).first.click(timeout=5_000),
    ]
    last_exc: Exception | None = None
    for strategy in strategies:
        try:
            strategy()
            return
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"Could not select radio '{company_size}': {last_exc}") from last_exc


def _read_confirmation(page: Page) -> str:
    # Modern Google Forms shows confirmation at this selector.
    selectors = [
        ".freebirdFormviewerViewResponseConfirmationMessage",
        "[data-confirmation-message]",
        "div.vHW8K",  # alternate class seen in some locales
    ]
    for sel in selectors:
        el = page.locator(sel)
        if el.count() > 0:
            text = el.first.text_content(timeout=5_000)
            if text and text.strip():
                return text.strip()

    # Fallback: if the URL changed to the response page, treat as success.
    if "formResponse" in page.url:
        return "Form submitted successfully"

    return "Submitted (confirmation text not found)"
