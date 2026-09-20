const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 720, height: 1080 }, deviceScaleFactor: 2 });
  await page.goto('file://' + process.argv[2]);
  await page.waitForTimeout(300);
  await page.locator('.card').screenshot({ path: process.argv[3] });
  await browser.close();
})();
