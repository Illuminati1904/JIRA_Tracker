// Headless-Chromium smoke test for a generated report: no JS errors in a US time zone, screenshots of key slides.
// usage: node scripts/ui_check.js <report.html> <out-prefix>   (Playwright must be installed)
const { chromium } = require('playwright');
(async () => {
  const f = process.argv[2], out = process.argv[3];
  const b = await chromium.launch();
  const ctx = await b.newContext({ timezoneId: 'America/Los_Angeles', viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push('pageerror: ' + e.message));
  p.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  await p.goto('file://' + f); await p.addStyleTag({content:'html{scroll-behavior:auto!important;scroll-snap-type:none!important}'});
  await p.waitForTimeout(1500);
  for (const id of ['s-hero', 's-head', 's-gate', 's-mem', 's-move']) {
    await p.evaluate(i => document.getElementById(i).scrollIntoView(), id);
    await p.waitForTimeout(300);
    await p.screenshot({ path: `${out}_${id}.png` });
  }
  // open a drawer
  await p.evaluate(() => document.querySelector('[data-k]').click());
  await p.waitForTimeout(400);
  await p.screenshot({ path: `${out}_drawer.png` });
  const dates = await p.evaluate(() => document.getElementById('win').textContent);
  console.log('win:', dates);
  console.log('errors:', JSON.stringify(errs));
  await b.close();
})();
