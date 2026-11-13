#!/usr/bin/env node
// Optional visual check: needs Playwright + a Chromium build.
//   npm i -D playwright && npx playwright install chromium
//   ./run.sh start && node tests/visual.mjs
// Not part of the unit test run; the app itself has no JS build step.
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://127.0.0.1:8765";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const problems = [];
page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
page.on("pageerror", (e) => problems.push(e.message));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForSelector(".entry-card");

// Open the first article.
await page.locator(".entry-card").first().click();
await page.waitForSelector(".detail-title");
await page.screenshot({ path: "/tmp/topicfeed-detail.png", fullPage: false });

// Open a revised article and its diff.
const revised = page.locator(".entry-card", { hasText: "个修订" }).first();
if (await revised.count()) {
  await revised.click();
  await page.waitForSelector("#open-diff");
  await page.click("#open-diff");
  await page.waitForSelector(".diff-line");
  await page.screenshot({ path: "/tmp/topicfeed-diff.png" });
}

// Error diagnostics visible in the feed sidebar.
const errorLine = page.locator(".feed-error").first();
const hasError = (await errorLine.count()) > 0;
await page.screenshot({ path: "/tmp/topicfeed-sidebar.png" });

console.log(JSON.stringify({
  detailShot: "/tmp/topicfeed-detail.png",
  diffShot: (await page.locator("#modal:not([hidden])").count()) ? "/tmp/topicfeed-diff.png" : null,
  sidebarShot: "/tmp/topicfeed-sidebar.png",
  feedErrorVisible: hasError,
  consoleProblems: problems,
}, null, 2));

await browser.close();
process.exit(problems.length ? 1 : 0);
