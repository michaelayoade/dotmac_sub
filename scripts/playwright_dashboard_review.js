const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      console.error("browser-console-error:", msg.text());
    }
  });

  await page.goto("http://127.0.0.1:8001/admin/login", { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.fill('input[name="username"], input[type="text"]', "admin");
  await page.fill('input[name="password"], input[type="password"]', "admin123");
  await page.click('button[type="submit"], button:has-text("Sign in"), button:has-text("Login")');
  await page.waitForLoadState("networkidle", { timeout: 30000 });

  await page.goto("http://127.0.0.1:8001/admin/dashboard", { waitUntil: "networkidle", timeout: 30000 });

  await page.screenshot({ path: "screenshots/dashboard-live-review.png", fullPage: true });
  console.log("title:", await page.title());
  console.log("url:", page.url());

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
