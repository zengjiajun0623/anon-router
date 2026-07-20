# Mainnet USDC flip

The ETH deposit path remains available. Enabling USDC requires configuration and
a redeploy of `CreditVault`; it does not require another code change.

## Deploy and configure

Deploy `CreditVault` with Ethereum mainnet USDC as its constructor argument:

```text
0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48
```

Then configure the deployment environment and restart the router/watcher's
deployment (do not reuse a testnet cursor file):

```sh
export CHAIN_RPC="https://YOUR_MAINNET_RPC"
export VAULT_ADDRESS="0xYOUR_NEW_CREDIT_VAULT"
export USDC_ADDRESS="0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48"
export CREDITS_PER_USDC="10000"
```

`CREDITS_PER_USDC=10000` means one USDC (1,000,000 base units) buys 10,000
credits when `CREDIT_USD=0.0001`. With `USDC_ADDRESS` unset, the watcher remains
ETH-only. For an ETH-only vault deployment, pass the zero address as the
constructor argument.

## Frontend deposit

This ethers v6 snippet approves the exact USDC amount and then submits
`depositUSDC(bytes32,uint256)`. `encodeFunctionData` produces the required
calldata as the 4-byte selector followed by the ABI-encoded `keyHash` and
`amount`.

```js
import { Contract, Interface, parseUnits } from "ethers";

const USDC = "0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48";
const VAULT = "0xYOUR_NEW_CREDIT_VAULT";
const amount = parseUnits("10", 6); // 10 USDC; USDC has 6 decimals
const keyHash = "0xYOUR_32_BYTE_KEY_HASH";

const usdc = new Contract(USDC, [
  "function approve(address spender,uint256 amount) returns (bool)",
], signer);
await (await usdc.approve(VAULT, amount)).wait();

const vaultInterface = new Interface([
  "function depositUSDC(bytes32 keyHash,uint256 amount)",
]);
// data = 0x7c34c355 selector + encoded keyHash + encoded amount
const data = vaultInterface.encodeFunctionData("depositUSDC", [keyHash, amount]);
await (await signer.sendTransaction({ to: VAULT, data })).wait();
```

For staging, Circle's Sepolia testnet USDC address is
`0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`. Verify the current address at
[developers.circle.com](https://developers.circle.com/) before deployment,
because Circle rotates testnet addresses.

## Counsel and mainnet gates

Before accepting real funds:

- Obtain counsel review of money-transmission, payments, sanctions/OFAC,
  AML/KYC, consumer-protection, privacy, tax, and terms/refund obligations in
  every served jurisdiction.
- Complete an independent smart-contract and watcher security audit, including
  ERC-20 behavior, key management, event finality/reorg handling, idempotency,
  accounting, and incident-response testing.
- Use a hardware-backed or multisig owner, documented sweep controls, least
  privilege, monitored RPC/provider redundancy, and tested key rotation.
- Set production confirmations and orphan reconciliation, monitor deposits,
  credits, balances, and sweeps, and rehearse pause/incident/recovery procedures.
- Reconcile on-chain USDC liabilities to issued credits and upstream reserves;
  define limits, alerts, refund policy, disclosures, and operational ownership.
- Validate the mainnet USDC and vault addresses independently, run a small-value
  canary, and obtain explicit legal, security, finance, and operations sign-off.
