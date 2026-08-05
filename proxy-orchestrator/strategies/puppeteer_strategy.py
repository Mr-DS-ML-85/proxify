"""
Puppeteer Strategy — Node.js Puppeteer with stealth plugin via subprocess.
Uses puppeteer-extra + puppeteer-extra-plugin-stealth.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse

from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.cookie_jar import CookieManager
from services.ua_pool import ua_pool
from engine.dom_cleaner import clean_dom

logger = logging.getLogger(__name__)

# Inline Node.js script for Puppeteer stealth fetch
_PUPPETEER_SCRIPT = """
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());

(async () => {
    const config = JSON.parse(process.argv[2]);
    let browser;
    try {
        browser = await puppeteer.launch({
            headless: 'new',
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ],
        });

        const page = await browser.newPage();

        // Random viewport
        const viewports = [
            {width: 1920, height: 1080},
            {width: 1366, height: 768},
            {width: 1440, height: 900},
        ];
        const vp = viewports[Math.floor(Math.random() * viewports.length)];
        await page.setViewport(vp);

        // Set user agent
        if (config.userAgent) {
            await page.setUserAgent(config.userAgent);
        }

        // Block unnecessary resources
        await page.setRequestInterception(true);
        page.on('request', (req) => {
            const blocked = ['image', 'media', 'font', 'stylesheet'];
            if (blocked.includes(req.resourceType())) {
                req.abort();
            } else {
                req.continue();
            }
        });

        // Set extra headers
        if (config.headers) {
            await page.setExtraHTTPHeaders(config.headers);
        }

        // Navigate (GET) or fetch (all other methods)
        let html, statusCode, finalUrl;
        const method = (config.method || "GET").toUpperCase();
        if (method === "GET") {
            const response = await page.goto(config.url, {
                waitUntil: config.waitUntil || 'domcontentloaded',
                timeout: config.timeout || 30000,
            });
            statusCode = response ? response.status() : 0;
            finalUrl = page.url();
            html = await page.content();
        } else {
            const resp = await page.evaluate(
                `(url, m, body, headers) => fetch(url, {
                    method: m,
                    headers: headers,
                    body: body || undefined,
                    redirect: 'follow'
                }).then(r => r.text())`,
                config.url, method, config.body || null, config.headers || {}
            );
            statusCode = 200;
            finalUrl = config.url;
            html = resp;
        }

        // Wait for Google results if applicable (GET only)
        if (method === "GET" && config.url.includes('google.') && config.url.includes('/search')) {
            try {
                await page.waitForSelector('h3', {timeout: 10000});
            } catch (e) {}
        }

        // Small delay
        await new Promise(r => setTimeout(r, 500));

        const cookies = await page.cookies();
        const cookieDict = {};
        cookies.forEach(c => { cookieDict[c.name] = c.value; });

        const result = {
            success: true,
            status_code: statusCode,
            final_url: finalUrl,
            html: html,
            cookies: cookieDict,
        };

        process.stdout.write(JSON.stringify(result));
    } catch (error) {
        const result = {
            success: false,
            status_code: 0,
            error: error.message,
            html: '',
            cookies: {},
        };
        process.stdout.write(JSON.stringify(result));
    } finally {
        if (browser) await browser.close();
    }
})();
"""


class PuppeteerStrategy(BaseStrategy):
    """
    Puppeteer stealth fetcher via Node.js subprocess.
    Requires: npm install puppeteer puppeteer-extra puppeteer-extra-plugin-stealth
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._available = False
        self._script_path: Optional[str] = None

    @property
    def name(self) -> str:
        return "puppeteer"

    @property
    def priority(self) -> int:
        return 40

    async def initialize(self) -> None:
        """Check if Node.js and Puppeteer are available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "node", "-e", "require('puppeteer-extra')",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                # Write script to temp file
                fd, path = tempfile.mkstemp(suffix=".js", prefix="puppeteer_fetch_")
                with os.fdopen(fd, "w") as f:
                    f.write(_PUPPETEER_SCRIPT)
                self._script_path = path
                self._available = True
                logger.info("PuppeteerStrategy initialized (puppeteer-extra available)")
            else:
                logger.warning(
                    f"Puppeteer not available: {stderr.decode()[:200]}"
                )
        except FileNotFoundError:
            logger.warning("Node.js not found. PuppeteerStrategy disabled.")
        except Exception as e:
            logger.warning(f"PuppeteerStrategy init error: {e}")

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Fetch via Puppeteer subprocess."""
        start_time = time.monotonic()

        if not self._available or not self._script_path:
            return self._make_result(
                request, start_time,
                success=False, error="Puppeteer not available",
            )

        try:
            user_agent = ua_pool.get_random()

            # Build config for the Node script
            node_config = {
                "url": request.url,
                "userAgent": user_agent,
                "method": request.method.upper() if request.method else "GET",
                "body": request.body.decode("utf-8", errors="replace") if request.body else None,
                "timeout": int(request.timeout * 1000),
                "waitUntil": "networkidle0" if "google." in request.url else "domcontentloaded",
                "headers": {
                    k: v for k, v in request.headers.items()
                    if k.lower() not in ("user-agent", "host", "connection")
                },
            }

            config_json = json.dumps(node_config)

            proc = await asyncio.create_subprocess_exec(
                "node", self._script_path, config_json,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.timeout + 10,
            )

            if proc.returncode != 0:
                error_msg = stderr.decode()[:500] if stderr else "Unknown error"
                return self._make_result(
                    request, start_time,
                    success=False, error=f"Puppeteer process failed: {error_msg}",
                )

            # Parse JSON result
            result_data = json.loads(stdout.decode())

            if not result_data.get("success"):
                return self._make_result(
                    request, start_time,
                    success=False,
                    error=result_data.get("error", "Unknown Puppeteer error"),
                )

            html = result_data.get("html", "")
            status_code = result_data.get("status_code", 0)
            cookies = result_data.get("cookies", {})

            # DOM cleaning for all responses
            if html:
                cleaned = clean_dom(html, url=request.url)
                if cleaned.success:
                    html = cleaned.clean_html
                    if cleaned.google_results:
                        logger.info(
                            f"Puppeteer Google DOM cleaned — "
                            f"{len(cleaned.google_results)} results"
                        )

            # Store cookies
            domain = urlparse(request.url).netloc
            if cookies:
                await self._cookie_manager.set_cookies(domain, cookies)

            result = self._make_result(
                request, start_time,
                success=200 <= status_code < 400 and len(html) > 100,
                status_code=status_code,
                final_url=result_data.get("final_url", request.url),
                html=html,
                cookies=cookies,
            )

            if result.is_blocked:
                result.success = False
                logger.debug(f"PuppeteerStrategy: blocked on {request.url}")

            return result

        except asyncio.TimeoutError:
            return self._make_result(
                request, start_time,
                success=False, error="Puppeteer timeout",
            )
        except Exception as e:
            logger.warning(f"PuppeteerStrategy error: {request.url}: {e}")
            return self._make_result(
                request, start_time,
                success=False, status_code=0, error=str(e),
            )

    async def shutdown(self) -> None:
        """Clean up temp script."""
        if self._script_path and os.path.exists(self._script_path):
            try:
                os.unlink(self._script_path)
            except Exception:
                pass
