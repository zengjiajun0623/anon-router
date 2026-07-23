/**
 * Smoke-test inline deposit errors (replaces alert()).
 * Run: node web/test_deposit_errors.mjs
 * Needs: npx playwright, local router on :8402
 */
import { chromium } from 'playwright';

const BASE = process.env.ROUTER || 'http://127.0.0.1:8402';
let failed = 0;

function check(cond, msg) {
  if (!cond) {
    console.error('  FAIL:', msg);
    failed++;
  } else {
    console.log('  PASS:', msg);
  }
}

async function errText(page) {
  return page.locator('#deposit-err').innerText();
}

async function errVisible(page) {
  return page.locator('#deposit-err').evaluate((el) => el.classList.contains('visible'));
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();

await page.goto(BASE + '/');
await page.click('#mint');
await page.waitForSelector('#deposit-body:not(.hidden)');

// 1) Invalid amount
await page.fill('#amount', '0');
await page.click('#deposit');
check(await errVisible(page), 'invalid amount shows inline error');
check(
  /valid ETH amount/i.test(await errText(page))
    && /greater than zero/i.test(await errText(page)),
  'invalid amount copy has what + fix',
);

// 2) Clear on edit
await page.fill('#amount', '0.05');
check(!(await errVisible(page)), 'editing amount clears the error');

// 3) No wallet
await page.evaluate(() => { delete window.ethereum; });
await page.click('#deposit');
check(await errVisible(page), 'no-wallet shows inline error');
check(
  /No browser wallet found/i.test(await errText(page))
    && /MetaMask|Rabby|Coinbase/i.test(await errText(page)),
  'no-wallet copy tells how to fix',
);

// 4) User rejection (mock ethereum)
await page.evaluate(() => {
  window.ethereum = {
    request: async ({ method }) => {
      if (method === 'eth_requestAccounts') {
        const err = new Error('User rejected the request.');
        err.code = 4001;
        throw err;
      }
      throw new Error('unexpected ' + method);
    },
  };
});
await page.fill('#amount', '0.05');
await page.click('#deposit');
check(await errVisible(page), 'rejection shows inline error');
{
  const t = await errText(page);
  check(/cancelled the request/i.test(t), 'rejection: what happened');
  check(/Click Deposit again/i.test(t), 'rejection: how to fix');
}

// 5) Insufficient funds
await page.evaluate(() => {
  window.ethereum = {
    request: async ({ method }) => {
      if (method === 'eth_requestAccounts') return ['0x' + '11'.repeat(20)];
      if (method === 'eth_chainId') return '0xaa36a7'; // Sepolia
      if (method === 'eth_sendTransaction') {
        throw new Error('insufficient funds for gas * price + value');
      }
      if (method === 'wallet_switchEthereumChain') return null;
      throw new Error('unexpected ' + method);
    },
  };
});
await page.click('#deposit');
check(await errVisible(page), 'insufficient funds shows inline error');
{
  const t = await errText(page);
  check(/Not enough Sepolia ETH/i.test(t), 'funds: what happened');
  check(/faucet/i.test(t), 'funds: how to fix');
}

// 6) Wrong network after a "successful" switch
await page.evaluate(() => {
  window.ethereum = {
    request: async ({ method }) => {
      if (method === 'eth_requestAccounts') return ['0x' + '11'.repeat(20)];
      if (method === 'eth_chainId') return '0x1'; // still on mainnet
      if (method === 'wallet_switchEthereumChain') return null; // pretends ok
      throw new Error('unexpected ' + method);
    },
  };
});
await page.click('#deposit');
check(await errVisible(page), 'wrong-network shows inline error');
{
  const t = await errText(page);
  check(/wrong network/i.test(t), 'wrong-network: what happened');
  check(/Switch to Sepolia/i.test(t), 'wrong-network: how to fix');
}

// 7) No alert() was used
let alertHit = false;
page.on('dialog', async (d) => { alertHit = true; await d.dismiss(); });
await page.evaluate(() => { delete window.ethereum; });
await page.fill('#amount', '0.05');
await page.click('#deposit');
await page.waitForTimeout(100);
check(!alertHit, 'deposit path does not call alert()');

await browser.close();
console.log(failed ? `\n${failed} failure(s)` : '\nall deposit-error checks passed');
process.exit(failed ? 1 : 0);
