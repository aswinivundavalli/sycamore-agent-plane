// Screenshot the conductor UI. Usage: node scripts/field-test/screenshot.js out.png [runId|-] [review|activity|task|log] [port]
const { chromium } = require('playwright');
(async () => {
  const [out, id, tab, port = '7700'] = process.argv.slice(2);
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  await p.goto(`http://127.0.0.1:${port}/` + (id && id !== '-' ? '#run-' + id : '')); await p.waitForTimeout(2500);
  if (tab) { await p.locator(`#d-tabs button[data-tab="${tab}"]`).click().catch(()=>{}); await p.waitForTimeout(2000); }
  await p.screenshot({ path: out }); await b.close();
})();
