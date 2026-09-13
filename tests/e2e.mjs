import { chromium } from 'playwright';

const BASE = 'http://127.0.0.1:8001';
const errors = [];
const failures = [];

function fail(label, detail) {
  failures.push(`${label}: ${detail}`);
  console.log(`FAIL: ${label} -- ${detail}`);
}
function ok(label) {
  console.log(`OK: ${label}`);
}

const browser = await chromium.launch();
const page = await browser.newPage();

page.on('console', msg => {
  if (msg.type() === 'error') errors.push(msg.text());
});
page.on('pageerror', err => {
  errors.push('pageerror: ' + err.message);
});

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(500);

// ---- Wrong password must actually be rejected now (real auth, not fake) ----
await page.click('text=Counsellor Login');
await page.waitForTimeout(200);
await page.fill('input[type="password"]', 'wrong-password');
await page.click('button:has-text("Sign in")');
await page.waitForTimeout(800);
if (await page.locator('text=Invalid email or password').count() === 0) fail('wrong-password-rejected', 'no error toast shown for a bad password');
else ok('wrong-password-rejected');
if (await page.locator('text=Priority cases').count() > 0) fail('wrong-password-rejected-no-access', 'app let us in despite bad password');
else ok('wrong-password-rejected-no-access');

// ---- Now sign in for real with the correct demo credentials ----
await page.click('button:has-text("Enter Counsellor Demo")');
await page.waitForTimeout(800);
if (await page.locator('text=Priority cases').count() === 0) fail('counsellor-login', 'Priority cases panel not found after login');
else ok('counsellor-login');

// ---- Create a new case ----
await page.click('button:has-text("New case")');
await page.waitForTimeout(300);
const newCaseModal = page.locator('div.fixed:has(h3:has-text("Register new case"))');
const newCaseInputs = newCaseModal.locator('input');
await newCaseInputs.nth(0).fill('Playwright Test Case');
await newCaseInputs.nth(1).fill('Nagpur');
await newCaseInputs.nth(2).fill('Maharashtra');
await newCaseModal.locator('button:has-text("Create case")').click();
await page.waitForTimeout(1000);
const toastText = await page.locator('text=registered for Playwright Test Case').count();
if (toastText === 0) fail('create-case', 'success toast not shown');
else ok('create-case');

if (await page.locator('text=Playwright Test Case').count() === 0) fail('create-case-visible', 'new case not visible in priority list / detail panel');
else ok('create-case-visible');

// ---- Switch to Asha (7934) and check override flow ----
const ashaButton = page.locator('button:has-text("Asha K.")').first();
if (await ashaButton.count() > 0) {
  await ashaButton.click();
  await page.waitForTimeout(400);
  await page.click('button:has-text("Override AI assessment")');
  await page.waitForTimeout(300);
  const overrideModal = page.locator('div.fixed:has(h3:has-text("Override AI assessment"))');
  await overrideModal.locator('input[type="number"]').fill('55');
  await overrideModal.locator('select').selectOption({ label: 'Moderate' });
  await overrideModal.locator('textarea').fill('Playwright automated override test');
  await overrideModal.locator('button:has-text("Save override")').click();
  await page.waitForTimeout(1000);
  if (await page.locator('text=Assessment overridden').count() === 0) fail('override', 'success toast not shown');
  else ok('override');
} else {
  fail('find-asha', 'Asha K. button not found in priority list');
}

// ---- Export report ----
const exportBtn = page.locator('button:has-text("Export report")').first();
if (await exportBtn.count() > 0) {
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 5000 }).catch(e => null),
    exportBtn.click(),
  ]);
  if (!download) fail('export-report', 'no download event fired');
  else ok('export-report (download triggered: ' + download.suggestedFilename() + ')');
} else {
  fail('export-report-button', 'Export report button not found');
}

// ---- Interventions tab (scoped to the case-detail tab bar, not the sidebar nav item of the same name) ----
await page.click('button.capitalize:has-text("interventions")');
await page.waitForTimeout(300);
if (await page.locator('text=Intervention history').count() === 0) fail('interventions-tab', 'tab did not render');
else ok('interventions-tab');

// ---- Notes tab: add a note, then reload the page to prove it's backend-persisted, not local state ----
await page.click('button.capitalize:has-text("notes")');
await page.waitForTimeout(300);
await page.fill('input[placeholder="Add a case note…"]', 'Playwright test note');
await page.click('button:has-text("Add")');
await page.waitForTimeout(800);
if (await page.locator('text=Playwright test note').count() === 0) fail('add-note', 'note not visible after adding');
else ok('add-note');

await page.reload({ waitUntil: 'networkidle' });
await page.waitForTimeout(800);
await page.click('text=Counsellor Login');
await page.waitForTimeout(200);
await page.click('text=Enter Counsellor Demo');
await page.waitForTimeout(800);
await page.click('button.capitalize:has-text("notes")');
await page.waitForTimeout(300);
if (await page.locator('text=Playwright test note').count() === 0) fail('note-persists-after-reload', 'note disappeared after full page reload — not actually backend-persisted');
else ok('note-persists-after-reload');

// ---- Alerts page (admin) ----
await page.click('button:has-text("Sign out")');
await page.waitForTimeout(300);
await page.click('text=Administration Portal');
await page.waitForTimeout(200);
await page.click('text=Enter Administrator Demo');
await page.waitForTimeout(800);
if (await page.locator('text=Raahat').count() === 0) fail('admin-login', 'admin app did not load');
else ok('admin-login');

await page.click('button:has-text("Alerts")');
await page.waitForTimeout(500);
const resolveBtn = page.locator('button:has-text("Mark Resolved"):not([disabled])').first();
if (await resolveBtn.count() > 0) {
  await resolveBtn.click();
  await page.waitForTimeout(1000);
  if (await page.locator('text=Alert marked resolved').count() === 0) fail('resolve-alert', 'success toast not shown');
  else ok('resolve-alert');
} else {
  fail('resolve-alert-button', 'no Mark Resolved button found (no real alerts?)');
}

// ---- People & Cases page: table is deliberately anonymized (ID/age/gender, no name) ----
await page.click('button:has-text("People & Cases")');
await page.waitForTimeout(500);
if (await page.locator('td:has-text("7935")').count() === 0 && await page.locator('td:has-text("7934")').count() === 0) {
  fail('people-page-list', 'no real backend case IDs shown in People & Cases table');
} else ok('people-page-list');

// ---- Newly fixed Counsellor sidebar nav: All Cases / Interventions / Reports ----
await page.click('button:has-text("Sign out")');
await page.waitForTimeout(300);
await page.click('text=Counsellor Login');
await page.waitForTimeout(200);
await page.click('text=Enter Counsellor Demo');
await page.waitForTimeout(800);

await page.click('nav button:has-text("All Cases")');
await page.waitForTimeout(400);
if (await page.locator('text=All cases').count() === 0) fail('counsellor-all-cases-nav', 'All Cases nav did not switch content');
else ok('counsellor-all-cases-nav');

await page.click('nav button:has-text("Interventions")');
await page.waitForTimeout(400);
if (await page.locator('h1:has-text("Interventions")').count() === 0) fail('counsellor-interventions-nav', 'Interventions nav did not switch content');
else ok('counsellor-interventions-nav');

await page.click('nav button:has-text("Reports")');
await page.waitForTimeout(400);
if (await page.locator('text=Risk Trends').count() === 0) fail('counsellor-reports-nav', 'Reports nav did not switch content');
else ok('counsellor-reports-nav');

await page.click('nav button:has-text("Dashboard")');
await page.waitForTimeout(400);
if (await page.locator('text=Priority cases').count() === 0) fail('counsellor-back-to-dashboard', 'Dashboard nav did not return to main view');
else ok('counsellor-back-to-dashboard');

// ---- Victim flow: chat with an urgent Hindi message + one-tap SOS ----
await page.click('button:has-text("Sign out")');
await page.waitForTimeout(300);
await page.click('text=Get Support');
await page.waitForTimeout(200);
await page.click('text=Enter Victim Demo');
await page.waitForTimeout(800);
// The greeting is built from the signed-in session's own name now (it used to be the hardcoded
// string "Good morning, Asha." regardless of who signed in), so assert on that instead.
if (await page.locator('text=Hello, Asha.').count() === 0) fail('victim-login', 'victim home did not load');
else ok('victim-login');

await page.click('button[aria-label="Open support assistant"]');
await page.waitForTimeout(400);
await page.fill('input[placeholder="You can type here…"]', 'मुझे मार रहा है');
await page.keyboard.press('Enter');
await page.waitForTimeout(1500);
if (await page.locator('text=Your message indicates that you may need immediate support').count() === 0) {
  fail('victim-chat-hindi-urgent', 'Hindi urgent message did not trigger the emergency card in the chat widget');
} else ok('victim-chat-hindi-urgent');
await page.click('button[aria-label="Open support assistant"]');
await page.waitForTimeout(300);

const sosBtn = page.locator('button:has-text("I need help right now")');
if (await sosBtn.count() > 0) {
  await sosBtn.click();
  await page.waitForTimeout(1200);
  if (await page.locator('text=care team has been alerted immediately').count() === 0) fail('victim-sos', 'SOS confirmation banner not shown');
  else ok('victim-sos');
} else {
  fail('victim-sos-button', 'SOS button not found on victim home');
}

// ---- Victim check-in wizard (multi-step form) ----
await page.click('button:has-text("Check In")');
await page.waitForTimeout(400);
for (let step = 0; step < 5; step++) {
  const firstOption = page.locator('div.rounded-2xl button.text-left').first();
  await firstOption.click();
  await page.waitForTimeout(150);
  await page.click('button:has-text("Next")');
  await page.waitForTimeout(150);
}
// step 6 (multi-select support) — pick one option then submit
await page.locator('div.rounded-2xl button.text-left').first().click();
await page.waitForTimeout(150);
await page.click('button:has-text("Submit")');
await page.waitForTimeout(1200);
if (await page.locator('text=Thank you for checking in.').count() === 0) fail('victim-checkin-flow', 'completion screen not shown after submitting check-in');
else ok('victim-checkin-flow');

// ---- Admin "New case" button (the other NewCaseModal instance) ----
await page.click('button:has-text("Sign out")');
await page.waitForTimeout(300);
await page.click('text=Administration Portal');
await page.waitForTimeout(200);
await page.click('text=Enter Administrator Demo');
await page.waitForTimeout(800);
const adminNewCaseBtn = page.locator('button:has-text("New case")');
if (await adminNewCaseBtn.count() > 0) {
  await adminNewCaseBtn.click();
  await page.waitForTimeout(300);
  const adminModal = page.locator('div.fixed:has(h3:has-text("Register new case"))');
  const modalInputs = adminModal.locator('input');
  await modalInputs.nth(0).fill('Admin Flow Case');
  await modalInputs.nth(1).fill('Kanpur Nagar');
  await modalInputs.nth(2).fill('Uttar Pradesh');
  await adminModal.locator('button:has-text("Create case")').click();
  await page.waitForTimeout(1000);
  if (await page.locator('text=registered for Admin Flow Case').count() === 0) fail('admin-new-case', 'success toast not shown');
  else ok('admin-new-case');
} else {
  fail('admin-new-case-button', 'New case button not found in Admin Vulnerable cases panel');
}

// ---- DemoSwitcher: switching role pill should do a real re-login as that role, not just flip the screen ----
const switcherCounsellorBtn = page.locator('div.fixed.top-3 button:has-text("Counsellor")');
if (await switcherCounsellorBtn.count() > 0) {
  await switcherCounsellorBtn.click();
  await page.waitForTimeout(1000);
  if (await page.locator('text=Priority cases').count() === 0) fail('demo-switcher-role-swap', 'switching to Counsellor via the pill did not load the counsellor app');
  else ok('demo-switcher-role-swap');
} else {
  fail('demo-switcher-not-found', 'demo switcher pill not visible while logged in');
}

await browser.close();

console.log('\n--- Console/page errors captured ---');
if (errors.length === 0) console.log('none');
else errors.forEach(e => console.log('CONSOLE/PAGE ERROR:', e));

console.log('\n--- Summary ---');
console.log(`${failures.length} failure(s) found`);
process.exit(failures.length > 0 || errors.length > 0 ? 1 : 0);
