# Multi-Chain Meme Scanner V5

Read-only scanner across four chains: **Robinhood Chain**, **Solana**, **BNB Smart Chain**, and **Base**.

## Strategy
300K+ observed peak → 55–80% retracement → current MC $75K–$125K → liquidity/activity checks → community/wallet proxy → risk-adjusted score.

The scoring model itself is chain-agnostic — the same setup/community/risk formulas apply everywhere. What differs per chain is how wallet-level "community" data is gathered.

## Rug / safety gate

Every qualifying token now goes through a safety check before it's scored, modeled on what GoPlus, RugCheck, and GMGN all check first:

- **EVM (BNB, Base):** [GoPlus Security's Token Security API](https://gopluslabs.io) (free tier, no key required) — honeypot status, buy/sell tax, mint function, hidden/reclaimable ownership, blacklist/pause functions, top-10 holder concentration, and LP lock/burn %. **Not supported for Robinhood Chain** (too small/new a chain for GoPlus's coverage) — those tokens are flagged `UNKNOWN`, never silently treated as safe.
- **Solana:** native JSON-RPC, no key required — mint authority and freeze authority status (unrevoked authorities are a classic rug vector) plus top-10 holder concentration via `getTokenLargestAccounts`. Set `RUGCHECK_API_KEY` (from rugcheck.xyz) to layer in RugCheck's bundler/sniper/insider-wallet detection and LP-lock analysis on top — that clustering analysis is genuinely hard to replicate from scratch and RugCheck already does it well for Solana.

The result is a **verdict, not just another number**: `SAFE`, `CAUTION`, or `DANGER`.
- `DANGER` tokens (honeypot, can't-sell-all, extreme concentration, reclaimable ownership) are **dropped from scan results entirely** — they never reach the table.
- `CAUTION` tokens (moderate concentration, high tax, mint authority still active, thin LP lock) stay visible but their final score is capped at 60/100, with the specific reasons shown.
- `UNKNOWN` means the safety data wasn't available (unsupported chain, API hiccup) — shown as unverified, never scored as if it were safe.

Tracked positions get the same check on every refresh, so if a token you're holding gets newly flagged, you'll see it — the app still won't sell for you, this is read-only.

## Sell alerts (positions + exit-risk signal)

Two things live in the **Positions & sell alerts** tab:

1. **Position tracking with a take-profit ladder.** Add a position (chain, token address, entry price, amount). Set the ladder in the sidebar — defaults to trim 25% at 2x, 25% at 3x, 25% at 5x, leaving a 25% moon bag, all adjustable. Hit "Refresh positions" and it tells you which tiers are due, with a "Mark sold" button that updates your remaining bag %. Nothing executes automatically — this is a read-only tool, so trimming is still on you.
2. **General exit-liquidity/distribution signal**, shown as an "Exit Risk" score (0–100) on *every* scanned token, whether you hold it or not, and refreshed for tracked positions too. It's built from data the scanner already has:
   - liquidity down sharply from its recent tracked peak (LPs pulling)
   - buy/sell ratio deteriorating versus recent history
   - 1h momentum reversing against the 6h trend
   - liquidity thin relative to market cap
   - wallet flow (from the Alchemy/Solana-RPC community proxy) turning net-negative, or just flipping negative

This is a heuristic built on public liquidity/volume/wallet-flow data, not a prediction or financial advice — it's meant to surface the same kind of thing you'd otherwise have to eyeball across several charts.

## Putting it online (so you can use it from a phone)

Running it locally means it only works while that computer is on and you're using it. To check it from an iPhone (or any phone) anytime, put it on Streamlit Community Cloud instead — free, and there's a 15-minute one-time setup using a computer, then you just visit a web link forever after.

1. **Create a free GitHub account** at github.com (just a place to store the code files, like Google Drive for code).
2. **Create a new repository** — click the **+** in the top right → **New repository**. Name it something like `meme-scanner`. **Set it to Private** (important — keeps your files from being public).
3. **Upload the files** — on the new repo's page, click **Add file → Upload files**, then drag in `app.py`, `README.md`, and `requirements.txt`. **Do not upload `.env`** (or `.env.example`) — API keys go in a different, safer place in step 5. Click **Commit changes**.
4. Go to **share.streamlit.io** and sign in with your GitHub account.
5. Click **New app**, pick your `meme-scanner` repo, and set the main file to `app.py`. Before deploying, open **Advanced settings → Secrets** and paste your keys in this format:
   ```
   ALCHEMY_API_KEY = "your key here"
   HELIUS_API_KEY = "your key here"
   RUGCHECK_API_KEY = "your key here"
   ```
   (Leave out any you don't have — nothing here is required.)
6. Click **Deploy**. After a minute or two, you get a permanent link like `https://your-app-name.streamlit.app`.
7. Open that link in Safari on your iPhone. To make it feel like a real app, tap the **Share** button → **Add to Home Screen** — now it's an icon on your phone like any other app.

**Two honest limitations of the free hosting tier:** the app "falls asleep" after a period of no visits and takes 10-30 seconds to wake back up on your next visit — normal, not broken. And the database that tracks peak-price history and your positions lives on that server's temporary storage, which can get wiped if the app restarts or redeploys — fine for trying it out, but not a permanent record. If that history matters long-term, that's a good V6 item (a proper hosted database instead of local SQLite).

## Getting API keys (step by step)

None of these are required to run the scanner — GoPlus (EVM safety checks) and the public Solana RPC both work with zero keys. Each key below just unlocks one extra data layer.

**Alchemy** (BNB Smart Chain + Base wallet-flow data — not usable for Robinhood Chain, which Alchemy doesn't support):
1. Sign up at alchemy.com.
2. In the dashboard, click **Create new app**.
3. Name it, and enable both **BNB Smart Chain** and **Base** on the same app.
4. Copy the **API key** from the app's page.

**Helius** (optional — faster Solana RPC than the public default):
1. Sign up at dashboard.helius.dev (free tier is enough to start).
2. Open **API Keys** in the sidebar → **Create New API Key**.
3. Copy it immediately — it's shown only once.

**RugCheck** (optional — adds Solana bundler/sniper/insider-wallet detection and LP-lock data):
1. Sign up at rugcheck.xyz and verify your email.
2. Generate an API key from your account's API section.

**Storing keys:** copy `.env.example` to `.env` in the same folder as `app.py` and fill in what you have:
```bash
cp .env.example .env
# then edit .env with your keys
```
The app loads `.env` automatically on startup — no need to `export` variables in your shell every session. Never commit `.env` to git.

## New all-time-high alerts for tracked positions

DexScreener's own API has no historical/candle endpoint (confirmed against its docs) — it only returns a live snapshot. So "all-time high" alerts use two sources combined:

1. **Our own tracked peak** — the highest market cap this scanner has personally observed since it started watching the token (same peak-tracking used for the setup filter).
2. **DexPaprika's free OHLCV history** (no API key needed) — covers Solana, BNB, and Base pool history back to the pool's creation. **Not available for Robinhood Chain**, since it isn't one of DexPaprika's 35 indexed networks — positions on that chain fall back to our own tracking only, and the app says so plainly rather than pretending otherwise.

Whichever of the two is higher is treated as the "best-known all-time high." Since DexPaprika's absolute price units aren't confirmed to be USD, the app doesn't trust its raw price directly — instead it takes the *ratio* between the highest historical candle and the most recent candle's close, then scales our own trusted DexScreener market cap by that ratio. This keeps the estimate honest about being built from two different sources rather than a single precise number.

On the **Positions & sell alerts** tab, refreshing a position that's at or above its best-known all-time high shows a 🚀 banner and adds "NEW ATH" to that position's header — the actual "you might want to sell some now" signal this was built for. It never sells anything automatically; it's read-only, same as everything else here.

## EVM bundle detection & deployer history (built after real-world research into GMGN/Bubblemaps/RugCheck)

Two additions closing gaps identified by comparing this scanner against GMGN, Photon, BullX, Bubblemaps, and RugCheck's own methodology:

- **EVM bundle detection (BNB, Base)** — the EVM analog of the Solana bundler check. Groups token recipients by the block they first received the token in; a block where many distinct wallets appear together is the classic fingerprint of a bundled/sniped launch (the same pattern Bubblemaps calls a "bundle"). Uses the same Alchemy transfer data already being fetched for the Community Score, so no new API calls. Limited to the ~8000-block lookback window already in place, so it catches recent bundled activity within that window, not necessarily the token's original launch block if it's older than that.
- **Deployer/creator history (EVM)** — GoPlus reports each token's creator wallet address. The scanner now keeps its own local registry of which creator wallets it has seen before and what verdict their other tokens got. If a creator wallet is linked to other tokens this scanner has flagged DANGER, any new token from that same wallet is flagged DANGER too, regardless of how clean the new contract looks on its own. This is honestly limited by design: it only knows what it has personally scanned, so it starts empty for every wallet and gets more useful the longer the scanner runs — it will never catch a repeat offender on its very first sighting.

Not pursued, on purpose: sub-second new-pair feeds and copy-trading smart-money wallets, since that puts this in trading-bot territory (Photon/Trojan/BullX's actual job) rather than a screening/scoring tool. Also not pursued: social/X sentiment signals — every mainstream tool has some version of this, but it's the easiest signal to fake and the hardest to do honestly without a paid data source, so it's a lower-priority addition than the two above.

## Efficiency (parallel scanning + auto-scan)

- **Parallel chain scanning**: the 4 chains used to be checked one after another; now their discovery data is fetched concurrently (one thread per chain), which meaningfully cuts total scan time. Scoring and database writes still happen one at a time afterward, since SQLite connections aren't safe to share across threads — but that part was never the slow piece anyway.
- **Auto-scan**: check "Auto-scan in the background" in the sidebar and set an interval (default every 15 minutes) to have it scan on its own while the tab stays open — no more manually clicking Scan Now to build up peak history. Requires the `streamlit-autorefresh` package (already in requirements.txt).

## Reliability (retry/backoff + surfaced failures)

Every external call (DexScreener, Alchemy, Solana RPC, GoPlus, RugCheck) now retries up to 3 times with exponential backoff on timeouts, connection errors, and 429/5xx responses — a bad address or unsupported chain (4xx) still fails fast instead of wasting retries. Failures that survive the retries are no longer silent: after a scan or a position refresh, an "N data call(s) failed" expander lists what didn't come through, so a quiet API hiccup doesn't look identical to "this token has no data."

## What's new in V5
- **Multi-chain**: pick any combination of Robinhood Chain, Solana, BNB, and Base from the sidebar; each scan runs the same discovery → scoring pipeline per chain.
- **Solana gets a native data path, not a port of the EVM one.** Instead of using Alchemy's `alchemy_getAssetTransfers` (EVM-only), Solana wallet activity is derived from Solana's own JSON-RPC: `getSignaturesForAddress` on the token mint, then `getTransaction` per signature, reading `preTokenBalances`/`postTokenBalances` deltas to see which wallet *owners* gained or lost the token in each transaction. This is closer to a real balance-change signal than the EVM proxy (which just counts raw from/to addresses).
- EVM chains (Robinhood Chain, BNB, Base) keep the original Alchemy-based transfer indexing, just parameterized by chain instead of hardcoded to Robinhood.
- SQLite tables now key on `(chain, pair/token)` so history doesn't collide across chains.

## Install on Mac
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in whatever API keys you have — see below
streamlit run app.py
```

## Optional indexed data
```bash
# EVM chains (Robinhood Chain / BNB / Base) — shared key across Alchemy-supported networks
export ALCHEMY_API_KEY="YOUR_KEY"

# Solana — optional, swaps the public RPC for Helius's higher-throughput RPC endpoint
export HELIUS_API_KEY="YOUR_KEY"

# Optional: override the default public Solana RPC if you're not using Helius
export SOLANA_RPC_URL="https://your-preferred-endpoint"

# Optional: how many recent signatures to inspect per Solana token (default 60)
export SOLANA_TX_LIMIT=60

streamlit run app.py
```

Notes:
- Without `ALCHEMY_API_KEY`, EVM chains fall back to a provisional Community Score (50/100, flagged as unavailable data).
- Without `HELIUS_API_KEY`, Solana uses the public `api.mainnet-beta.solana.com` endpoint, which is rate-limited — expect slower scans and possibly a lower `SOLANA_TX_LIMIT` in practice. A Helius (or other Solana RPC provider) key is recommended for real use.
- Robinhood Chain's Alchemy network slug (`robinhood-mainnet`) is carried over from the original build; confirm it's a network Alchemy actually supports on your account, since it's a newer/smaller chain.
- Community data is a proxy, not an exact holder count or proof of a healthy community — this is true on every chain here, including the Solana balance-delta approach.

Never put a wallet private key in this program.

## Next
- True holder-balance snapshots and top-holder concentration, on both EVM (via periodic balance polling) and Solana (via `getTokenLargestAccounts` / `getProgramAccounts`, extending the balance-delta groundwork already in V5).
- Deployer/LP analysis and DEX swap-level wallet classification, per chain.
- Alerts, paper trading, and backtesting before any execution layer.
