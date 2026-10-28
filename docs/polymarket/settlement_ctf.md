# Settlement, collateral, wallets, positions

Read 2026-10-27. Tags as in trading_rules.md.

## 1. Collateral: pUSD

* Since 2026-04-28 pUSD replaces USDC.e: ERC-20 on Polygon, 6 decimals, backed 1:1 by USDC with backing enforced
  on-chain, no plans to list it elsewhere [official] concepts/pusd.
* Wrap with CollateralOnramp `0x93070a847efEf7F70739046A929D47a521F5B8ee`, unwrap with CollateralOfframp
  `0x2957922Eb93258b93368531d39fAcCA3B4dC5854`; pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` [official] resources/contracts.
* Rebates, rewards and referral payouts are paid in pUSD [official].

## 2. Exchange contracts (Polygon)

| Contract | Address |
|---|---|
| CTF Exchange (V2) | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg Risk CTF Exchange | `0xe2222d279d744050d28e00520010520000310F59` |
| Conditional Tokens | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| ExchangeV3 (Protocol V2 markets) | `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` |
| AutoRedeemer | `0xa1200000d0002264C9a1698e001292D00E1b00af` |
| Deposit Wallet Factory / Beacon | `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07` / `0x7A18EDfe055488A3128f01F563e5B479D92ffc3a` |

Source: [official] resources/contracts. EIP-712 exchange domain version "2" for CTF orders (ClobAuth stays "1");
Protocol V2 orders use domain version "3" [official].

## 3. Lifecycle of a fill

Orders are matched off-chain by the operator and settled on-chain atomically; settlement may transfer tokens, split
pUSD into complete sets, or merge sets back into pUSD [official] concepts/order-lifecycle. Trade states
MATCHED → MINED → CONFIRMED (or RETRYING/FAILED). Since 2026-07-24 FAK/FOK responses give `tradeIDs` only; hashes
come later (`wait_for_order_fill_settlement`, default 30 s) [official]. "Ghost fills" after V2: MATCHED with no
settlement for 1-50+ s or never [GitHub] py-clob-client-v2 #34.

## 4. Split, merge, redeem

* Split pUSD → one of each outcome; merge a complete set → pUSD; redeem winners for 1 pUSD each after resolution;
  **no redemption deadline** [official] trading/positions/manage.
* BTC Up/Down markets are `version: v1` (Conditional Tokens), `negRisk: false` [ours].
* **Automatic redemption exists**: AutoRedeemer redeems resolved CTF and Protocol V2 balances; notifications
  `AUTO_REDEEMED` (type 9) / `COMBO_AUTO_REDEEMED` (10); activity still shows `REDEEM` [official] contracts,
  onchain-position-data; [GitHub] SDK notification enum and changelog. Deposit Wallet setup approves the current
  auto-redeem operator (SDK 0.1.0b16 note). Timing of auto-redemption is not documented.
* Since 2026-08-10 `/activity` reports a redemption **per outcome** (two rows when both sides are redeemed; the losing
  one has `usdcSize 0`) [official] changelog.
* Gasless operations go through the Relayer (`POST /submit` returns at once with `transactionID`, hash via
  `GET /transaction`, since 2026-04-21) [official]. Relayer `/submit` limit 25/min per IP [official]; unverified
  builders get 100 relayer transactions/day [official] builders/tiers.

## 5. Wallets

* Deposit Wallet = signature type 3 (`POLY_1271`, ERC-1271/7739 signatures); EOA 0, Proxy 1, Safe 2 [official].
* V2 cutover wiped all resting orders (2026-04-28); py-clob-client v1 SDKs stopped working [official] changelog.
* "maker address not allowed" root cause: V2 needs the deposit-wallet flow for new API users [GitHub]
  py-clob-client-v2 #111.
* Session keys: owner-authorised signer, ≤180 days, scopes `CLOB` / `COMBOSRFQ`; revocation cancels that key's open
  orders [official] trading/session-keys.
* Beacon upgrades for Deposit Wallets deployed after 2026-06-29; opt-out possible [official].

## 6. Protocol V2 (new position system)

New markets can use PositionManager positions instead of CTF tokens: trade via ExchangeV3, balances
`CONDITIONAL-V2`, split/merge/redeem via Router. Existing CTF holdings are not converted; migration CTF → V2 only for
conditions Polymarket registers [official] migrate/polymarket-v2. BTC Up/Down are still CTF (`v1`) today; when new
crypto markets appear as `v2`, our order building, approvals and redemption need the V2 path (SDK ≥ 0.9.0 handles it,
`setup_trading_approvals()` includes V2 modules) [GitHub].
