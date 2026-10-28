# Latency and infrastructure

Read 2026-10-27. Tags as in trading_rules.md.

## 1. Where Polymarket runs

* "**Primary Servers**: eu-west-2" (AWS London). "**Closest Non-Georestricted Region**: eu-west-1" (Ireland)
  [official] api-reference/geoblock and help.polymarket.com "Geographic Restrictions" (updated 2026-08-14).
* `clob.polymarket.com` sits behind Cloudflare [official] rate-limits ("Cloudflare IP-based request limits");
  [third-party] Glassnode HyperLatency about page. From Dublin, TCP connect to the edge is 1.0 ms and a kept-alive
  HTTPS GET of `/time` 24 ms p50 [ours, docs/latency/README.md §3]: the edge is local, the origin is in London.
* Staging host: `https://clob-staging.polymarket.com` [official] clob-openapi `servers`.
* Endpoints: CLOB REST `clob.polymarket.com`; market/user WS `wss://ws-subscriptions-clob.polymarket.com/ws/{market,user}`;
  PolyBolt prices `wss://ws-live-v2.polymarket.com/ws`; legacy RTDS `wss://ws-live-data.polymarket.com`; Gamma
  `gamma-api.polymarket.com`; Data `data-api.polymarket.com` (`/v2`); Bridge `bridge.polymarket.com`;
  geoblock `polymarket.com/api/geoblock` [official].

## 2. Co-location (the rule we missed)

> "**Direct co-location form.** Users who complete the KYC/KYB form can get access to co-locate directly in
> `eu-west-2` for the lowest possible latency to Polymarket's primary servers."
> "Co-location is available to market makers and individual traders operating on their own account. It is not
> available to Builder Program participants or third-party applications executing trades on behalf of end users."
> — [official] api-reference/geoblock, section "Server Infrastructure and Co-Location"

* Form: `https://docs.google.com/forms/d/e/1FAIpQLSfY-3Dl3yxq8HKFjFad8YzKZmm0k3Gdg29HD6gL-K-AmI6KXw/viewform` [official link].
* In the docs since about 2026-05-13 [third-party] bitcoinfoundation.org; reported by The Information on 2026-05-27
  as Polymarket "offering faster trading speeds" to users who share identifying information [third-party].
* Not published: cost, approval time, what "access" is (permission to trade from London IPs despite the UK
  close-only rule, or a private network path past Cloudflare), the latency it gives. See open_questions.md Q2.
* Eligibility as written: own-account traders and market makers. Our user trades their own account from Canada
  (Manitoba, not a restricted province) → eligible on the face of the text.

## 3. What co-location would buy us (estimate)

| Path | Order/cancel RTT | Source |
|---|---|---|
| Home PC (Winnipeg) | ~130 ms | [ours] dublin-server memory |
| Dublin (eu-west-1) today | CLOB HTTPS 24 ms p50; post-only ack p50 65 / p90 177; cancel p50 61 / p90 156 | [ours] latency profile |
| London (eu-west-2) | TTFB 16-18 ms measured once from London; third-party claims ~1 ms inside the region | [ours] + [third-party] |

Network gain ≈ 6-20 ms per request at p50 if co-location still goes through Cloudflare, more if it is a direct
path. Most of our order latency (65 ms p50 ack) is server-side processing, which co-location does not change.
For takers the 150 ms delay and the ~1.7 s effective hold dwarf this. The main gains are (a) maker cancel races
and (b) a sanctioned location for the bots (see account_kyc_restrictions.md).

## 4. Taker delay as an infrastructure fact

150 ms on crypto Up/Down since 2026-09-04 [official]. The API holds the response until the hold ends and the order
is re-validated [official]. So a FAK's round trip ≥ network + 150 ms + processing. Our p50 FAK round trip 614 ms
[ours] is far above that; see trading_rules.md §3.

July 2026 "async commit pipeline cuts matching latency": since 2026-07-24 04:00 UTC FAK/FOK responses return
`tradeIDs` but no `transactionHashes` [official] changelog 2026-07-17.

## 5. WebSocket behaviour that affects latency

* Market and user channels: client sends text `PING` every 10 s, server replies `PONG` [official]. User channel: send
  the subscription frame immediately or the server may close the connection [official].
* Market channel `level` parameter: 1, 2 or 3, default 2, **undocumented** [official asyncapi]. A 12 s capture of a
  BTC 5m token at each level on 2026-10-27 showed the same message types and fields [ours].
* Several users report close code **1013 "slow consumer: send buffer full"** on BTC 5m market sockets even with
  fast consumers, and data stalls while PONGs continue (Stockholm, Mumbai, Windows) [GitHub] rs-clob-client-v2 #122
  (open, 2026-10-06..16). Our recorder should log 1013 closes and gaps.
* PolyBolt: server WS ping every 25 s, two missed pongs close with 4002; limits 64 subscriptions/connection,
  20 sub frames/s, 8 auth frames/connection, 64 KB frames; close 4003 = draining, reconnect after 0-10 s jitter
  [official] api-reference/live-data/overview.
* PolyBolt crypto payloads carry `timestamp` (source time) and equity payloads carry `received_at`; `seq` is dense per
  connection+channel; `dropped` counts frames dropped because the client lagged [official].
* Hosting advice from Polymarket: none beyond the region names above. Third-party vendor pages (NYCServers,
  TradoxVPS, Glassnode) agree London is fastest and Dublin the closest unrestricted region [third-party].
