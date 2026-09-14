import os, time, sqlite3, json, requests, concurrent.futures
from collections import Counter
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_autorefresh import st_autorefresh

load_dotenv()  # reads a .env file in the same folder, if present

# When deployed on Streamlit Community Cloud, keys live in st.secrets instead of a
# .env file -- copy any of ours found there into the environment so the rest of the
# app (which reads os.getenv everywhere) doesn't need to know the difference.
for _k in ('ALCHEMY_API_KEY', 'HELIUS_API_KEY', 'RUGCHECK_API_KEY', 'SOLANA_RPC_URL', 'SOLANA_TX_LIMIT'):
    try:
        if _k in st.secrets and not os.getenv(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass  # no secrets.toml present (e.g. running locally) -- fine, .env already covered it

DB = 'meme_scanner_v5.db'
DEX = 'https://api.dexscreener.com'

ALCHEMY_API_KEY = os.getenv('ALCHEMY_API_KEY', '')
HELIUS_API_KEY = os.getenv('HELIUS_API_KEY', '')
SOLANA_TX_LIMIT = int(os.getenv('SOLANA_TX_LIMIT', '60'))  # signatures inspected per Solana token
GOPLUS_APP_KEY = os.getenv('GOPLUS_APP_KEY', '')
GOPLUS_APP_SECRET = os.getenv('GOPLUS_APP_SECRET', '')
RUGCHECK_API_KEY = os.getenv('RUGCHECK_API_KEY', '')  # optional bonus layer for Solana bundler/sniper/LP-lock data

CHAINS = {
    'robinhood': {'label': 'Robinhood Chain', 'dex_id': 'robinhood', 'kind': 'evm', 'alchemy_slug': 'robinhood-mainnet', 'goplus_id': None, 'dexpaprika_id': None},
    'bsc':       {'label': 'BNB Smart Chain', 'dex_id': 'bsc',       'kind': 'evm', 'alchemy_slug': 'bnb-mainnet',       'goplus_id': '56', 'dexpaprika_id': 'bsc'},
    'base':      {'label': 'Base',            'dex_id': 'base',      'kind': 'evm', 'alchemy_slug': 'base-mainnet',      'goplus_id': '8453', 'dexpaprika_id': 'base'},
    'solana':    {'label': 'Solana',          'dex_id': 'solana',    'kind': 'solana', 'goplus_id': None, 'dexpaprika_id': 'solana'},
}

DEFAULT_LADDER = [(2.0, 25.0), (3.0, 25.0), (5.0, 25.0)]  # (multiple vs entry, % of original stack to trim)

S = requests.Session(); S.headers['User-Agent'] = 'Meme-Scanner-V5/1.0'
st.set_page_config(page_title='Meme Scanner V5', page_icon='🦊', layout='wide')

# ---------- storage ----------

def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute('''CREATE TABLE IF NOT EXISTS state(
        chain TEXT, pair TEXT, token TEXT, symbol TEXT, name TEXT, url TEXT,
        first_seen INT, last_seen INT, peak_mc REAL, peak_ts INT,
        PRIMARY KEY(chain, pair))''')
    c.execute('''CREATE TABLE IF NOT EXISTS snap(
        chain TEXT, pair TEXT, ts INT, mc REAL, price REAL, liq REAL, vol REAL,
        buys INT, sells INT, p1h REAL, p6h REAL, p24h REAL,
        PRIMARY KEY(chain, pair, ts))''')
    c.execute('''CREATE TABLE IF NOT EXISTS community(
        chain TEXT, token TEXT, ts INT, active INT, buyers INT, sellers INT, fresh INT, net INT,
        PRIMARY KEY(chain, token, ts))''')
    c.execute('''CREATE TABLE IF NOT EXISTS positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain TEXT, token TEXT, symbol TEXT,
        entry_price REAL, amount REAL, remaining_pct REAL,
        ladder_json TEXT, triggered_json TEXT,
        opened_ts INT, closed_ts INT, status TEXT DEFAULT 'open')''')
    c.execute('''CREATE TABLE IF NOT EXISTS deployers(
        chain TEXT, creator TEXT, token TEXT, symbol TEXT, first_seen INT, last_verdict TEXT,
        PRIMARY KEY(chain, creator, token))''')
    return c

# ---------- resilient HTTP (retry/backoff + non-silent failure tracking) ----------

SCAN_WARNINGS = []  # reset at the start of each scan()/refresh_positions() call

def _retry(fn, tries=3, base_delay=0.6):
    """Retry on timeouts/connection errors and 429/5xx only -- a 4xx like a bad
    address or unsupported chain fails fast instead of wasting three attempts."""
    last_err = None
    for attempt in range(tries):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            last_err = e
            if code and code < 500 and code != 429:
                raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
        if attempt < tries - 1:
            time.sleep(base_delay * (2 ** attempt))
    raise last_err

def http_get(url, **kwargs):
    def call():
        r = S.get(url, timeout=kwargs.pop('timeout', 20), **kwargs); r.raise_for_status(); return r
    return _retry(call)

def http_post(url, **kwargs):
    def call():
        r = S.post(url, timeout=kwargs.pop('timeout', 25), **kwargs); r.raise_for_status(); return r
    return _retry(call)

def warn(label):
    SCAN_WARNINGS.append(label)

# ---------- dexscreener discovery ----------

def dex(path, params=None):
    try:
        return http_get(DEX + path, params=params).json()
    except Exception as e:
        warn(f'DexScreener {path} failed: {type(e).__name__}')
        raise

def discover(chain_key):
    dex_id = CHAINS[chain_key]['dex_id']
    addrs = set()
    for f in ['/token-profiles/latest/v1', '/community-takeovers/latest/v1', '/token-boosts/latest/v1', '/token-boosts/top/v1']:
        try:
            for x in dex(f) or []:
                if x.get('chainId') == dex_id and x.get('tokenAddress'):
                    addrs.add(x['tokenAddress'])
        except Exception:
            pass
    for q in ['meme', 'dog', 'cat', 'frog', 'pepe', 'moon', 'ai', 'sol', 'bnb', 'base']:
        try:
            for p in dex('/latest/dex/search', {'q': q}).get('pairs', []):
                if p.get('chainId') == dex_id:
                    a = (p.get('baseToken') or {}).get('address')
                    if a:
                        addrs.add(a)
        except Exception:
            pass
    pairs = []; aa = list(addrs)
    for i in range(0, len(aa), 30):
        try:
            pairs += dex(f'/tokens/v1/{dex_id}/' + ','.join(aa[i:i + 30]))
        except Exception:
            pass
    best = {}
    for p in pairs:
        if p.get('chainId') != dex_id:
            continue
        a = (p.get('baseToken') or {}).get('address')
        l = float((p.get('liquidity') or {}).get('usd') or 0)
        if a and (a not in best or l > best[a][0]):
            best[a] = (l, p)
    return [v[1] for v in best.values()]

def fetch_token_pair(chain_key, token_address):
    """Look up a single token directly by address, regardless of whether discover()'s
    search-term heuristic would have surfaced it. Used to refresh tracked positions."""
    dex_id = CHAINS[chain_key]['dex_id']
    try:
        pairs = dex(f'/tokens/v1/{dex_id}/{token_address}') or []
    except Exception:
        return None
    best = None; best_liq = -1
    for p in pairs:
        if p.get('chainId') != dex_id:
            continue
        l = float((p.get('liquidity') or {}).get('usd') or 0)
        if l > best_liq:
            best_liq = l; best = p
    return best

def dexpaprika_true_ath(chain_key, pair_address, created_at_ms, current_mc, current_price):
    """DexScreener's own API has no historical/OHLC endpoint (confirmed against
    its docs), so this uses DexPaprika's free, keyless OHLCV endpoint instead --
    the only source found that covers Solana, BNB, and Base pool history without
    a paid plan. Not available for Robinhood Chain, which isn't among DexPaprika's
    35 indexed networks.

    Rather than trust DexPaprika's raw price units to be USD (unconfirmed), this
    takes the RATIO between the highest historical candle and the most recent
    candle's close, then scales our own trusted current DexScreener market cap by
    that ratio. That keeps the result honest about being an estimate built from
    two different data sources, not a precise historical market cap."""
    net = CHAINS[chain_key].get('dexpaprika_id')
    if not net or not pair_address or not current_price:
        return None
    start = time.strftime('%Y-%m-%d', time.gmtime(created_at_ms / 1000)) if created_at_ms else '2020-01-01'
    try:
        r = http_get(f'https://api.dexpaprika.com/networks/{net}/pools/{pair_address}/ohlcv',
                     params={'start': start, 'interval': '24h', 'limit': 1000})
        candles = r.json()
    except Exception as e:
        warn(f'DexPaprika OHLCV failed: {type(e).__name__}')
        return None
    if not candles:
        return None
    try:
        highest = max(float(c.get('high') or 0) for c in candles)
        latest_close = float(candles[-1].get('close') or 0)
    except (ValueError, TypeError):
        return None
    if not highest or not latest_close:
        return None
    return current_mc * (highest / latest_close)

def metrics(p, chain_key):
    h = (p.get('txns') or {}).get('h24') or {}; pc = p.get('priceChange') or {}
    b = int(h.get('buys') or 0); s = int(h.get('sells') or 0)
    return {
        'chain': chain_key,
        'pair': p.get('pairAddress', ''),
        'token': (p.get('baseToken') or {}).get('address', ''),
        'symbol': (p.get('baseToken') or {}).get('symbol', '?'),
        'name': (p.get('baseToken') or {}).get('name', '?'),
        'url': p.get('url', ''),
        'mc': float(p.get('marketCap') or p.get('fdv') or 0),
        'price': float(p.get('priceUsd') or 0),
        'liq': float((p.get('liquidity') or {}).get('usd') or 0),
        'vol': float((p.get('volume') or {}).get('h24') or 0),
        'buys': b, 'sells': s, 'txns': b + s,
        'p1h': float(pc.get('h1') or 0), 'p6h': float(pc.get('h6') or 0), 'p24h': float(pc.get('h24') or 0),
        'socials': len((p.get('info') or {}).get('socials') or []),
        'boosts': int((p.get('boosts') or {}).get('active') or 0),
        'created': int(p.get('pairCreatedAt') or 0),
    }

# ---------- EVM wallet/community proxy (Alchemy) ----------

def alchemy_rpc_url(chain_key):
    slug = CHAINS[chain_key].get('alchemy_slug')
    if not ALCHEMY_API_KEY or not slug:
        return None
    return f'https://{slug}.g.alchemy.com/v2/{ALCHEMY_API_KEY}'

def alchemy_call(method, params, url):
    try:
        r = http_post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        return r.json().get('result')
    except Exception as e:
        warn(f'Alchemy {method} failed: {type(e).__name__}')
        raise

def evm_transfers(chain_key, token):
    url = alchemy_rpc_url(chain_key)
    if not url:
        return []
    try:
        latest = alchemy_call('eth_blockNumber', [], url)
        if not latest:
            return []
        start = max(0, int(latest, 16) - 8000); page = None; out = []
        for _ in range(4):
            q = {'fromBlock': hex(start), 'toBlock': 'latest', 'contractAddresses': [token],
                 'category': ['erc20'], 'excludeZeroValue': True, 'withMetadata': False, 'maxCount': 1000}
            if page:
                q['pageKey'] = page
            z = alchemy_call('alchemy_getAssetTransfers', [q], url)
            if not z:
                break
            out += z.get('transfers', []); page = z.get('pageKey')
            if not page:
                break
        return out
    except Exception:
        return []

def community_evm(transfers):
    t = transfers or []
    if not t:
        return {'active': None, 'buyers': None, 'sellers': None, 'fresh': None, 'net': None}
    send = {x.get('from', '').lower() for x in t if x.get('from')}
    recv = {x.get('to', '').lower() for x in t if x.get('to')}
    active = send | recv
    counts = Counter(x.get('to', '').lower() for x in t if x.get('to'))
    return {'active': len(active), 'buyers': len(recv), 'sellers': len(send),
            'fresh': sum(n == 1 for n in counts.values()), 'net': len(recv) - len(send)}

def evm_bundle_signal(transfers):
    """EVM analog of Solana bundler detection: groups transfer recipients by the
    block they first appear in. A block where many distinct wallets receive the
    token together is the fingerprint of a bundled/sniped launch (same pattern
    Bubblemaps calls a 'bundle'). Limited to our ~8000-block lookback window, so
    this catches recent bundled activity, not necessarily the token's original
    launch block if it's older than that window."""
    if not transfers:
        return {'bundled': False, 'launch_block_wallets': 0, 'total_wallets_seen': 0}
    first_block = {}
    for t in transfers:
        to = (t.get('to') or '').lower()
        blk = t.get('blockNum')
        if not to or not blk:
            continue
        try:
            blk_int = int(blk, 16)
        except (TypeError, ValueError):
            continue
        if to not in first_block or blk_int < first_block[to]:
            first_block[to] = blk_int
    if not first_block:
        return {'bundled': False, 'launch_block_wallets': 0, 'total_wallets_seen': 0}
    earliest_block = min(first_block.values())
    launch_block_wallets = sum(1 for b in first_block.values() if b == earliest_block)
    return {'bundled': launch_block_wallets >= 5, 'launch_block_wallets': launch_block_wallets,
            'total_wallets_seen': len(first_block)}

# ---------- Solana wallet/community proxy (native RPC, no Alchemy) ----------

def solana_rpc_url():
    if HELIUS_API_KEY:
        return f'https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}'
    return os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')

def solana_call(method, params, url):
    try:
        r = http_post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        return r.json().get('result')
    except Exception as e:
        warn(f'Solana RPC {method} failed: {type(e).__name__}')
        raise

def solana_transfers(mint):
    """Uses getSignaturesForAddress + getTransaction on the mint itself, then reads
    pre/post SPL token-balance deltas to infer which wallet owners gained or lost
    the token in each transaction. This is a Solana-native proxy, not a port of the
    EVM/Alchemy approach."""
    url = solana_rpc_url()
    try:
        sigs = solana_call('getSignaturesForAddress', [mint, {'limit': SOLANA_TX_LIMIT}], url) or []
    except Exception:
        return []
    out = []
    for s in sigs:
        sig = s.get('signature')
        if not sig:
            continue
        try:
            tx = solana_call('getTransaction', [sig, {'maxSupportedTransactionVersion': 0, 'encoding': 'jsonParsed'}], url)
        except Exception:
            continue
        if not tx:
            continue
        meta = tx.get('meta') or {}
        pre = {b['accountIndex']: b for b in (meta.get('preTokenBalances') or []) if b.get('mint') == mint}
        post = {b['accountIndex']: b for b in (meta.get('postTokenBalances') or []) if b.get('mint') == mint}
        for idx, pb in post.items():
            owner = pb.get('owner')
            if not owner:
                continue
            pre_amt = float((pre.get(idx, {}).get('uiTokenAmount') or {}).get('uiAmount') or 0)
            post_amt = float((pb.get('uiTokenAmount') or {}).get('uiAmount') or 0)
            delta = post_amt - pre_amt
            if delta > 0:
                out.append({'owner': owner, 'direction': 'in'})
            elif delta < 0:
                out.append({'owner': owner, 'direction': 'out'})
    return out

def community_solana(mint):
    t = solana_transfers(mint)
    if not t:
        return {'active': None, 'buyers': None, 'sellers': None, 'fresh': None, 'net': None}
    owners_in = [x['owner'] for x in t if x['direction'] == 'in']
    owners_out = [x['owner'] for x in t if x['direction'] == 'out']
    active = set(owners_in) | set(owners_out)
    counts = Counter(owners_in)
    return {'active': len(active), 'buyers': len(set(owners_in)), 'sellers': len(set(owners_out)),
            'fresh': sum(n == 1 for n in counts.values()), 'net': len(set(owners_in)) - len(set(owners_out))}

def community(chain_key, token, transfers=None):
    if CHAINS[chain_key]['kind'] == 'solana':
        return community_solana(token)
    return community_evm(transfers if transfers is not None else evm_transfers(chain_key, token))

# ---------- scoring (chain-agnostic) ----------

def setup(x, peak):
    r = 1 - x['mc'] / peak if peak else 1; s = 0; w = []
    if peak >= 300000 and .55 <= r <= .80: s += 30; w.append('ideal 300K+ retracement')
    elif peak >= 300000 and .45 <= r <= .85: s += 18; w.append('qualifying retracement')
    if x['liq'] >= 50000: s += 15; w.append('strong liquidity')
    elif x['liq'] >= 25000: s += 10; w.append('adequate liquidity')
    if x['vol'] >= 100000: s += 12; w.append('high volume')
    elif x['vol'] >= 10000: s += 7; w.append('meaningful volume')
    if x['buys'] / max(x['sells'], 1) >= 1.2: s += 10; w.append('buy pressure')
    elif x['buys'] / max(x['sells'], 1) >= .9: s += 5; w.append('balanced flow')
    if x['socials'] >= 2: s += 5; w.append('multiple social links')
    elif x['socials']: s += 2; w.append('social link')
    if x['boosts']: s += 3; w.append('active boost')
    if x['p1h'] > -15: s += 5; w.append('1h not collapsing')
    if x['p6h'] > -35: s += 5; w.append('6h structure intact')
    if x['mc'] and x['liq'] / x['mc'] >= .2: s += 5; w.append('healthy liquidity/MC')
    return min(100, s), w

def community_score(c):
    if c['active'] is None:
        return 50, ['indexed wallet data unavailable']
    s = 0; w = []
    if c['active'] >= 500: s += 25; w.append('large active wallet base')
    elif c['active'] >= 200: s += 18; w.append('healthy active wallet base')
    elif c['active'] >= 75: s += 10; w.append('active wallets')
    ratio = c['buyers'] / max(c['sellers'], 1)
    if ratio >= 1.25: s += 25; w.append('more receiving than sending wallets')
    elif ratio >= .95: s += 15; w.append('balanced wallet flow')
    if c['fresh'] and c['active']:
        p = c['fresh'] / c['active']
        if p >= .35: s += 20; w.append('fresh wallet participation')
        elif p >= .15: s += 10; w.append('new wallet participation')
    if c['net'] > 0: s += 10; w.append('positive transfer-side flow')
    if min(c['buyers'], c['sellers']) >= 100: s += 20; w.append('two-sided wallet participation')
    elif min(c['buyers'], c['sellers']) >= 30: s += 10; w.append('two-sided participation')
    return min(100, s), w

def risk(x, c):
    s = 0; w = []
    if x['liq'] < 20000: s += 30; w.append('thin liquidity')
    elif x['liq'] < 30000: s += 15; w.append('borderline liquidity')
    if x['mc'] and x['liq'] / x['mc'] < .12: s += 20; w.append('low liquidity/MC')
    if x['sells'] > x['buys'] * 1.75: s += 20; w.append('heavy sell pressure')
    elif x['sells'] > x['buys'] * 1.25: s += 10; w.append('sell pressure')
    if x['p1h'] < -25: s += 15; w.append('fresh 1h collapse')
    if x['p6h'] < -45: s += 15; w.append('broken 6h structure')
    if c['active'] is not None and c['active'] < 50: s += 15; w.append('weak wallet activity')
    return min(100, s), w

def goplus_evm_security(chain_key, token):
    """GoPlus Security Token Security API — free tier, no key required (optional
    GOPLUS_APP_KEY/SECRET for higher rate limits, not implemented here since the
    public endpoint covers our needs). Returns None if the chain isn't supported
    (e.g. Robinhood Chain) or the call fails."""
    gid = CHAINS[chain_key].get('goplus_id')
    if not gid:
        return None
    try:
        r = http_get(f'https://api.gopluslabs.io/api/v1/token_security/{gid}',
                     params={'contract_addresses': token.lower()})
        result = (r.json().get('result') or {})
        return result.get(token.lower()) or next(iter(result.values()), None)
    except Exception as e:
        warn(f'GoPlus lookup failed: {type(e).__name__}')
        return None

def solana_mint_authorities(mint):
    url = solana_rpc_url()
    try:
        acc = solana_call('getAccountInfo', [mint, {'encoding': 'jsonParsed'}], url)
        info = (((acc or {}).get('value') or {}).get('data') or {}).get('parsed', {}).get('info', {})
        return {'mint_authority': info.get('mintAuthority'), 'freeze_authority': info.get('freezeAuthority'),
                'supply': info.get('supply'), 'decimals': info.get('decimals')}
    except Exception:
        return None

def solana_top_holder_pct(mint, supply_raw, decimals):
    url = solana_rpc_url()
    try:
        res = solana_call('getTokenLargestAccounts', [mint], url)
        vals = (res or {}).get('value') or []
        amounts = [float(v.get('uiAmount') or 0) for v in vals]
        total = float(supply_raw) / (10 ** (decimals or 0)) if supply_raw else sum(amounts)
        return (sum(amounts[:10]) / total * 100) if total else None
    except Exception:
        return None

def rugcheck_report(mint):
    if not RUGCHECK_API_KEY:
        return None
    try:
        r = http_get(f'https://api.rugcheck.xyz/v1/tokens/{mint}/report',
                     headers={'X-API-KEY': RUGCHECK_API_KEY})
        return r.json()
    except Exception as e:
        warn(f'RugCheck lookup failed: {type(e).__name__}')
        return None

def safety_check(chain_key, token, transfers=None):
    """Rug/honeypot/concentration gate, modeled on what GoPlus, RugCheck, and GMGN
    all check before anything else: can you actually sell it, does someone control
    the supply, and is it dangerously concentrated. Returns a SAFE/CAUTION/DANGER
    verdict plus reasons -- verdict drives whether scan() keeps or drops a result,
    it is not just folded into the blended score."""
    reasons = []; danger = False; caution = False
    if CHAINS[chain_key]['kind'] == 'evm':
        g = goplus_evm_security(chain_key, token)
        if g is None:
            return {'verdict': 'UNKNOWN', 'reasons': ['safety data unavailable for this chain/token'], 'source': 'none', 'creator': None}
        if g.get('is_honeypot') == '1':
            danger = True; reasons.append('flagged as a honeypot (can buy, cannot sell)')
        if g.get('cannot_sell_all') == '1':
            danger = True; reasons.append('cannot sell full balance in one transaction')
        sell_tax = float(g.get('sell_tax') or 0) * 100
        if sell_tax >= 50:
            danger = True; reasons.append(f'extreme sell tax ({sell_tax:.0f}%)')
        elif sell_tax >= 10:
            caution = True; reasons.append(f'high sell tax ({sell_tax:.0f}%)')
        if g.get('hidden_owner') == '1' or g.get('can_take_back_ownership') == '1':
            danger = True; reasons.append('owner can hide or reclaim contract control')
        if g.get('is_mintable') == '1':
            caution = True; reasons.append('supply is mintable')
        if g.get('is_blacklisted') == '1' or g.get('transfer_pausable') == '1':
            caution = True; reasons.append('blacklist or transfer-pause function present')
        holders = g.get('holders') or []
        top10 = sum(float(h.get('percent') or 0) for h in holders[:10]) * 100 if holders else None
        if top10 is not None:
            if top10 >= 80:
                danger = True; reasons.append(f'top 10 holders control {top10:.0f}% of supply')
            elif top10 >= 40:
                caution = True; reasons.append(f'top 10 holders control {top10:.0f}% of supply')
        lp = g.get('lp_holders') or []
        if lp:
            locked_pct = sum(float(h.get('percent') or 0) for h in lp if h.get('is_locked') in (1, '1', True)) * 100
            if locked_pct < 50:
                caution = True; reasons.append(f'only ~{locked_pct:.0f}% of LP is locked/burned')
        bundle = evm_bundle_signal(transfers or [])
        if bundle['bundled']:
            if bundle['launch_block_wallets'] >= 15:
                danger = True
                reasons.append(f"{bundle['launch_block_wallets']} wallets first received this token in the same block in our recent lookback window (likely bundled/sniped buying)")
            else:
                caution = True
                reasons.append(f"{bundle['launch_block_wallets']} wallets first received this token in the same block in our recent lookback window (possible bundled buying)")
        verdict = 'DANGER' if danger else ('CAUTION' if caution else 'SAFE')
        return {'verdict': verdict, 'reasons': reasons or ['no red flags found'], 'top10_pct': top10,
                'source': 'GoPlus', 'creator': g.get('creator_address')}
    else:
        auth = solana_mint_authorities(token)
        if auth is None:
            return {'verdict': 'UNKNOWN', 'reasons': ['Solana mint data unavailable'], 'source': 'none', 'creator': None}
        if auth.get('mint_authority'):
            caution = True; reasons.append('mint authority not revoked (supply can be inflated)')
        if auth.get('freeze_authority'):
            caution = True; reasons.append('freeze authority not revoked (balances can be frozen)')
        top10 = solana_top_holder_pct(token, auth.get('supply'), auth.get('decimals'))
        if top10 is not None:
            if top10 >= 80:
                danger = True; reasons.append(f'top 10 holders control {top10:.0f}% of supply')
            elif top10 >= 40:
                caution = True; reasons.append(f'top 10 holders control {top10:.0f}% of supply')
        source = 'Solana RPC'
        rc = rugcheck_report(token)
        if rc:
            source += ' + RugCheck'
            if rc.get('rugged'):
                danger = True; reasons.append('RugCheck flags this token as already rugged')
            for item in (rc.get('risks') or []):
                lvl = (item.get('level') or '').lower()
                desc = item.get('name') or item.get('description') or 'flagged risk'
                if lvl == 'danger':
                    danger = True; reasons.append(f'RugCheck: {desc}')
                elif lvl in ('warn', 'warning'):
                    caution = True; reasons.append(f'RugCheck: {desc}')
        verdict = 'DANGER' if danger else ('CAUTION' if caution else 'SAFE')
        return {'verdict': verdict, 'reasons': reasons or ['no red flags found'], 'top10_pct': top10, 'source': source, 'creator': None}

def distribution_signal(c, chain_key, x, community_now):
    """Heuristic 'are you at risk of being exit liquidity' score (0-100, higher = more
    warning signs). Built entirely from read-only data already in this scanner:
    liquidity draining off its recent peak, buy/sell ratio deteriorating vs recent
    history, 1h momentum reversing against the 6h trend, thin liquidity/MC, and
    wallet flow (from the same community proxy) turning net-negative. This is a
    heuristic, not a prediction or financial advice."""
    s = 0; w = []
    rows = c.execute('SELECT ts, mc, liq, buys, sells FROM snap WHERE chain=? AND pair=? ORDER BY ts DESC LIMIT 30',
                      (chain_key, x['pair'])).fetchall()
    if rows:
        peak_liq = max(r[2] for r in rows) or x['liq']
        if peak_liq and x['liq'] < peak_liq * 0.75:
            s += 25; w.append(f"liquidity down {100*(1 - x['liq']/peak_liq):.0f}% from its recent peak")
        if len(rows) >= 3:
            earliest = rows[-1]
            prev_ratio = earliest[3] / max(earliest[4], 1)
            now_ratio = x['buys'] / max(x['sells'], 1)
            if now_ratio < prev_ratio * 0.7:
                s += 20; w.append('buy/sell ratio deteriorating vs recent history')
    if x['p6h'] > 0 and x['p1h'] < 0:
        s += 15; w.append('1h momentum reversing against the 6h trend')
    if x['mc'] and x['liq'] / x['mc'] < 0.10:
        s += 15; w.append('liquidity thin relative to market cap')
    if community_now.get('net') is not None:
        if community_now['net'] < 0:
            s += 15; w.append('wallet flow net negative (more sending than receiving wallets)')
        prev = c.execute('SELECT net FROM community WHERE chain=? AND token=? ORDER BY ts DESC LIMIT 1',
                          (chain_key, x['token'])).fetchone()
        if prev and prev[0] is not None and prev[0] >= 0 and community_now['net'] < 0:
            s += 10; w.append('wallet flow just flipped negative')
    return min(100, s), w

# ---------- deployer/creator reputation (EVM only -- GoPlus gives us creator_address) ----------

def deployer_history(c, chain_key, creator, exclude_token):
    """Tokens this same creator wallet has deployed that we've scanned before,
    other than the one we're currently checking. This is only as complete as our
    own scan history -- it grows more useful the longer the scanner runs, and
    starts out empty for every wallet, which is an honest limitation, not a bug."""
    if not creator:
        return []
    return c.execute('''SELECT token, symbol, last_verdict FROM deployers
        WHERE chain=? AND creator=? AND token!=?''',
        (chain_key, creator.lower(), exclude_token)).fetchall()

def record_deployer(c, chain_key, creator, token, symbol, verdict, now):
    if not creator:
        return
    c.execute('''INSERT INTO deployers VALUES(?,?,?,?,?,?)
        ON CONFLICT(chain, creator, token) DO UPDATE SET last_verdict=excluded.last_verdict''',
        (chain_key, creator.lower(), token, symbol, now, verdict))

def apply_deployer_check(c, chain_key, token, symbol, safety, now):
    """Folds deployer-reputation into an EVM safety verdict: if this creator wallet
    has other tokens in our own history flagged DANGER, that's a strong signal
    regardless of how clean this particular contract looks. Records this token's
    own verdict under the creator too, so the registry keeps improving over time."""
    creator = safety.get('creator')
    if creator:
        prior = deployer_history(c, chain_key, creator, token)
        danger_count = sum(1 for _, _, v in prior if v == 'DANGER')
        if danger_count:
            safety['verdict'] = 'DANGER'
            safety['reasons'].append(f'deployer wallet linked to {danger_count} other token(s) we flagged DANGER')
        elif len(prior) >= 3 and safety['verdict'] == 'SAFE':
            safety['verdict'] = 'CAUTION'
            safety['reasons'].append(f'deployer wallet has launched {len(prior)} other tokens we\'ve tracked')
        record_deployer(c, chain_key, creator, token, symbol, safety['verdict'], now)
    return safety

# ---------- shared snapshot recording ----------

def record_snapshot(c, chain_key, x, now):
    row = c.execute('SELECT peak_mc, peak_ts FROM state WHERE chain=? AND pair=?', (chain_key, x['pair'])).fetchone()
    peak = float(row[0]) if row else 0; pts = int(row[1]) if row else now
    if x['mc'] > peak:
        peak = x['mc']; pts = now
    c.execute('''INSERT INTO state VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chain, pair) DO UPDATE SET
            token=excluded.token, symbol=excluded.symbol, name=excluded.name,
            url=excluded.url, last_seen=excluded.last_seen,
            peak_mc=excluded.peak_mc, peak_ts=excluded.peak_ts''',
        (chain_key, x['pair'], x['token'], x['symbol'], x['name'], x['url'], now, now, peak, pts))
    c.execute('INSERT OR REPLACE INTO snap VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (chain_key, x['pair'], now, x['mc'], x['price'], x['liq'], x['vol'],
         x['buys'], x['sells'], x['p1h'], x['p6h'], x['p24h']))
    return peak

# ---------- scan ----------

def scan(chain_keys):
    SCAN_WARNINGS.clear()
    c = conn(); now = int(time.time()); out = []
    # Fetch each chain's discovery data in parallel -- this is the bulk of the
    # network calls in a scan, and they're independent per chain, so running
    # them concurrently instead of one-after-another cuts scan time a lot.
    discovered = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chain_keys) or 1) as ex:
        future_map = {ex.submit(discover, ck): ck for ck in chain_keys}
        for fut in concurrent.futures.as_completed(future_map):
            ck = future_map[fut]
            try:
                discovered[ck] = fut.result()
            except Exception as e:
                warn(f"{CHAINS[ck]['label']} discovery failed: {type(e).__name__}")
                discovered[ck] = []
    # Scoring + DB writes stay single-threaded (sqlite3 connections aren't
    # safe to share across threads) -- this part is fast/local anyway.
    for chain_key in chain_keys:
        for p in discovered.get(chain_key, []):
            x = metrics(p, chain_key)
            if not x['pair'] or not x['mc']:
                continue
            peak = record_snapshot(c, chain_key, x, now)
            age = (now - x['created'] / 1000) / 3600 if x['created'] else 99999
            if (peak >= 300000 and 75000 <= x['mc'] <= 125000 and x['liq'] >= 25000
                    and x['vol'] >= 10000 and x['txns'] >= 75 and age <= 720):
                is_evm = CHAINS[chain_key]['kind'] == 'evm'
                transfers = evm_transfers(chain_key, x['token']) if is_evm else None
                safety = safety_check(chain_key, x['token'], transfers)
                if is_evm:
                    safety = apply_deployer_check(c, chain_key, x['token'], x['symbol'], safety, now)
                if safety['verdict'] == 'DANGER':
                    continue  # hard gate: don't surface tokens flagged as honeypot/rug/extreme concentration/repeat-offender deployer
                cs = community(chain_key, x['token'], transfers)
                cscore, cwhy = community_score(cs)
                ss, swhy = setup(x, peak)
                rs, rwhy = risk(x, cs)
                dscore, dwhy = distribution_signal(c, chain_key, x, cs)
                final = round(.55 * ss + .30 * cscore + .15 * (100 - rs), 1)
                if safety['verdict'] == 'CAUTION':
                    final = min(final, 60.0)  # cap ranking for tokens with real but non-fatal red flags
                out.append({**x, 'peak': peak, 'retrace': 100 * (1 - x['mc'] / peak),
                            'setup': ss, 'community': cscore, 'risk': rs, 'final': final,
                            'dist_score': dscore, 'dist_why': dwhy, 'safety': safety,
                            'swhy': swhy, 'cwhy': cwhy, 'rwhy': rwhy, **cs})
                if cs['active'] is not None:
                    c.execute('INSERT OR REPLACE INTO community VALUES(?,?,?,?,?,?,?,?)',
                        (chain_key, x['token'], now, cs['active'], cs['buyers'], cs['sellers'], cs['fresh'], cs['net']))
    c.commit(); c.close()
    return sorted(out, key=lambda z: z['final'], reverse=True)

# ---------- positions ----------

def add_position(c, chain_key, token, symbol, entry_price, amount, ladder=None):
    now = int(time.time())
    c.execute('''INSERT INTO positions(chain, token, symbol, entry_price, amount, remaining_pct,
                    ladder_json, triggered_json, opened_ts, status)
                 VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (chain_key, token, symbol, entry_price, amount, 100.0,
         json.dumps(ladder) if ladder else None, json.dumps([]), now, 'open'))
    c.commit()

def close_position(c, pos_id):
    c.execute('UPDATE positions SET status=?, closed_ts=? WHERE id=?', ('closed', int(time.time()), pos_id))
    c.commit()

def mark_tier_sold(c, pos_id, tier_multiple, ladder):
    row = c.execute('SELECT remaining_pct, triggered_json FROM positions WHERE id=?', (pos_id,)).fetchone()
    if not row:
        return
    remaining, triggered_json = row
    triggered = json.loads(triggered_json) if triggered_json else []
    sell_pct = next((p for m, p in ladder if m == tier_multiple), 0)
    triggered.append(tier_multiple)
    new_remaining = max(0.0, remaining - sell_pct)
    c.execute('UPDATE positions SET remaining_pct=?, triggered_json=? WHERE id=?',
              (new_remaining, json.dumps(triggered), pos_id))
    c.commit()

def refresh_positions(c, default_ladder):
    SCAN_WARNINGS.clear()
    rows = c.execute('''SELECT id, chain, token, symbol, entry_price, amount, remaining_pct,
                                ladder_json, triggered_json
                         FROM positions WHERE status='open' ''').fetchall()
    now = int(time.time()); out = []
    for (pid, chain_key, token, symbol, entry_price, amount, remaining_pct, ladder_json, triggered_json) in rows:
        p = fetch_token_pair(chain_key, token)
        if not p:
            out.append({'id': pid, 'chain': chain_key, 'symbol': symbol, 'error': 'no market data found for this address'})
            continue
        x = metrics(p, chain_key)
        old_row = c.execute('SELECT peak_mc FROM state WHERE chain=? AND pair=?', (chain_key, x['pair'])).fetchone()
        old_tracked_peak = float(old_row[0]) if old_row else 0.0
        record_snapshot(c, chain_key, x, now)
        true_ath_mc = dexpaprika_true_ath(chain_key, x['pair'], x['created'], x['mc'], x['price'])
        best_known_peak = max(old_tracked_peak, true_ath_mc or 0)
        is_new_ath = best_known_peak > 0 and x['mc'] >= best_known_peak
        ath_source = 'DexPaprika history' if (true_ath_mc and true_ath_mc >= old_tracked_peak) else 'our own tracking'
        is_evm = CHAINS[chain_key]['kind'] == 'evm'
        transfers = evm_transfers(chain_key, token) if is_evm else None
        cs = community(chain_key, token, transfers)
        dscore, dwhy = distribution_signal(c, chain_key, x, cs)
        safety = safety_check(chain_key, token, transfers)
        if is_evm:
            safety = apply_deployer_check(c, chain_key, token, x['symbol'] or symbol, safety, now)
        if cs['active'] is not None:
            c.execute('INSERT OR REPLACE INTO community VALUES(?,?,?,?,?,?,?,?)',
                (chain_key, token, now, cs['active'], cs['buyers'], cs['sellers'], cs['fresh'], cs['net']))
        ladder = json.loads(ladder_json) if ladder_json else default_ladder
        triggered = json.loads(triggered_json) if triggered_json else []
        multiple = (x['price'] / entry_price) if entry_price else 0
        due = [tier for tier in ladder if tier[0] not in triggered and multiple >= tier[0]]
        out.append({'id': pid, 'chain': chain_key, 'symbol': x['symbol'] or symbol, 'token': token,
                    'entry_price': entry_price, 'price': x['price'], 'mc': x['mc'], 'liq': x['liq'], 'vol': x['vol'],
                    'multiple': multiple, 'remaining_pct': remaining_pct, 'ladder': ladder, 'triggered': triggered,
                    'due_tiers': due, 'dist_score': dscore, 'dist_why': dwhy, 'safety': safety, 'url': x['url'],
                    'is_new_ath': is_new_ath, 'best_known_peak': best_known_peak, 'ath_source': ath_source,
                    'true_ath_available': true_ath_mc is not None})
    c.commit()
    return out

# ---------- UI ----------

st.title('🦊 Multi-Chain Meme Scanner V5')
st.caption('300K+ peak → ~$100K retracement → wallet/community support → risk-adjusted ranking, across Robinhood Chain, Solana, BNB, and Base')

with st.sidebar:
    st.header('Chains')
    selected = st.multiselect('Scan these chains', options=list(CHAINS.keys()),
                               default=list(CHAINS.keys()), format_func=lambda k: CHAINS[k]['label'])
    st.divider()
    st.header('Strategy')
    st.write('Previous peak: **$300K+**')
    st.write('Current zone: **$75K–$125K**')
    st.write('Ideal retracement: **55–80%**')
    st.write('Minimum liquidity: **$25K**')
    st.write('Minimum 24h volume: **$10K**')
    st.write('Minimum 24h transactions: **75**')
    st.divider()
    st.header('Take-profit ladder')
    st.caption('Applies to new positions. Existing positions keep the ladder they were opened with.')
    l1c, l1p = st.columns(2)
    m1 = l1c.number_input('Tier 1 multiple', min_value=1.1, value=2.0, step=0.1, key='m1')
    p1 = l1p.number_input('Tier 1 sell %', min_value=0, max_value=100, value=25, step=5, key='p1')
    l2c, l2p = st.columns(2)
    m2 = l2c.number_input('Tier 2 multiple', min_value=1.1, value=3.0, step=0.1, key='m2')
    p2 = l2p.number_input('Tier 2 sell %', min_value=0, max_value=100, value=25, step=5, key='p2')
    l3c, l3p = st.columns(2)
    m3 = l3c.number_input('Tier 3 multiple', min_value=1.1, value=5.0, step=0.1, key='m3')
    p3 = l3p.number_input('Tier 3 sell %', min_value=0, max_value=100, value=25, step=5, key='p3')
    current_ladder = sorted([(m1, float(p1)), (m2, float(p2)), (m3, float(p3))])
    st.caption(f"Remaining bag after all tiers: {max(0, 100 - p1 - p2 - p3)}% (moon bag)")
    st.divider()
    st.header('Indexed data')
    if ALCHEMY_API_KEY:
        st.success('Alchemy ON — EVM chains (Robinhood Chain / BNB / Base)')
    else:
        st.warning('Alchemy OFF — EVM Community Score will be provisional')
    if HELIUS_API_KEY:
        st.success('Helius RPC ON — Solana')
    else:
        st.info('Solana using public RPC (rate-limited) — set HELIUS_API_KEY for higher throughput')
    st.divider()
    st.header('Rug / safety checks')
    st.success('GoPlus Security ON — BNB & Base (free tier, no key needed)')
    st.success('EVM bundle detection ON — flags same-block coordinated buying (BNB & Base, uses existing Alchemy data)')
    st.success('Deployer history ON — flags creator wallets linked to other DANGER-flagged tokens in our own scan history')
    st.caption('Not supported for Robinhood Chain — flagged as unverified, not treated as safe.')
    st.success('Solana mint/freeze authority + top-10 concentration ON (native RPC, no key needed)')
    if RUGCHECK_API_KEY:
        st.success('RugCheck ON — adds bundler/sniper/LP-lock data for Solana')
    else:
        st.info('Set RUGCHECK_API_KEY for bundler/sniper/insider + LP-lock detection on Solana')
    st.caption('Tokens flagged DANGER are dropped from results. CAUTION tokens are capped at 60/100 and shown with reasons.')
    st.divider()
    st.header('Auto-scan')
    auto_scan_on = st.checkbox('Auto-scan in the background while this tab stays open')
    auto_scan_minutes = st.number_input('Every N minutes', min_value=1, max_value=60, value=15, step=1, disabled=not auto_scan_on)
    manual_scan = st.button('🔎 SCAN NOW', type='primary')
    if auto_scan_on:
        st.caption("Runs automatically at this interval — you don't need to keep clicking Scan Now.")
        tick = st_autorefresh(interval=int(auto_scan_minutes * 60 * 1000), key='auto_scan_timer')
    else:
        tick = None
    do_scan = manual_scan or (auto_scan_on and st.session_state.get('_last_auto_tick') != tick)
    if do_scan:
        if not selected:
            st.error('Select at least one chain')
        else:
            if auto_scan_on:
                st.session_state['_last_auto_tick'] = tick
            with st.spinner('Scanning...'):
                st.session_state.r = scan(selected)
                st.session_state.scan_warnings = list(SCAN_WARNINGS)

tab_scan, tab_positions = st.tabs(['Scanner', 'Positions & sell alerts'])

with tab_scan:
    r = st.session_state.get('r', [])
    if not r:
        st.info('No qualifying setup yet. Run a scan and let the database build peak history.')
    else:
        t = r[0]
        a, b, c, d, e = st.columns(5)
        a.metric('Best score', f"{t['final']}/100"); b.metric('Setup', f"{t['setup']}/100")
        c.metric('Community', f"{t['community']}/100"); d.metric('Risk', f"{t['risk']}/100")
        e.metric('Current MC', f"${t['mc']:,.0f}")

        st.dataframe(pd.DataFrame([{
            'Score': x['final'], 'Chain': CHAINS[x['chain']]['label'], 'Token': x['symbol'],
            'Safety': x['safety']['verdict'],
            'MC': round(x['mc']), 'Peak': round(x['peak']), 'Retrace': f"{x['retrace']:.0f}%",
            'Setup': x['setup'], 'Community': x['community'], 'Risk': x['risk'],
            'Exit Risk': x['dist_score'],
            'Liquidity': round(x['liq']), 'Vol24': round(x['vol']), 'Active wallets': x['active'],
        } for x in r]), use_container_width=True, hide_index=True)
        st.caption('Tokens flagged DANGER by the safety check never reach this table — they\'re dropped during scanning.')
        sw = st.session_state.get('scan_warnings', [])
        if sw:
            with st.expander(f"⚠️ {len(sw)} data call(s) failed during this scan (retried, then skipped)"):
                for w in sw[:50]:
                    st.write('- ' + w)
                if len(sw) > 50:
                    st.write(f'...and {len(sw) - 50} more')

        for i, x in enumerate(r[:10], 1):
            label = CHAINS[x['chain']]['label']
            with st.expander(f"#{i} [{label}] {x['symbol']} — {x['final']}/100 | Safety {x['safety']['verdict']} | Setup {x['setup']} | Community {x['community']} | Risk {x['risk']} | Exit Risk {x['dist_score']}"):
                sv = x['safety']['verdict']
                safety_line = f"**Safety ({x['safety']['source']}):** " + (' • '.join(x['safety']['reasons']))
                if sv == 'CAUTION':
                    st.warning(safety_line)
                elif sv == 'UNKNOWN':
                    st.info(safety_line + ' — treat as unverified, not as safe.')
                else:
                    st.success(safety_line)
                st.write(f"**Peak:** ${x['peak']:,.0f} → **Current:** ${x['mc']:,.0f} ({x['retrace']:.1f}% retracement)")
                st.write(f"**Liquidity:** ${x['liq']:,.0f} | **24h volume:** ${x['vol']:,.0f} | **Buys/Sells:** {x['buys']}/{x['sells']}")
                st.write('**Setup:** ' + (' • '.join(x['swhy']) or 'none'))
                st.write('**Community:** ' + (' • '.join(x['cwhy']) or 'none'))
                st.write('**Risk:** ' + (' • '.join(x['rwhy']) or 'none'))
                if x['dist_score'] >= 40:
                    st.warning('**Exit-risk signal:** ' + (' • '.join(x['dist_why']) or 'elevated'))
                else:
                    st.write('**Exit risk:** ' + (' • '.join(x['dist_why']) or 'no warning signs yet'))
                if x['active'] is not None:
                    source = 'Solana RPC (balance-delta proxy)' if CHAINS[x['chain']]['kind'] == 'solana' else 'Alchemy transfer index'
                    st.write(f"**Wallet proxy ({source}):** {x['active']} active | {x['buyers']} receiving | {x['sellers']} sending | {x['fresh']} fresh-wallet proxy")
                if x['url']:
                    st.link_button('Open DexScreener', x['url'])
                if x['token'] not in {p['token'] for p in st.session_state.get('open_tokens', [])}:
                    with st.popover('➕ Track this as a position'):
                        ep = st.number_input('Entry price (USD)', min_value=0.0, value=float(x['price']), format='%f', key=f"ep_{x['pair']}")
                        amt = st.number_input('Amount held (tokens)', min_value=0.0, value=0.0, key=f"amt_{x['pair']}")
                        if st.button('Add position', key=f"add_{x['pair']}"):
                            cdb = conn()
                            add_position(cdb, x['chain'], x['token'], x['symbol'], ep, amt, current_ladder)
                            cdb.close()
                            st.success(f"Tracking {x['symbol']} — refresh the Positions tab to see it.")

    st.divider()
    st.warning('Read-only: no private keys, signing, or trade execution.')

with tab_positions:
    st.caption('Sell-tier and exit-risk alerts below are heuristics built from public liquidity, '
               'volume, and wallet-flow data — not financial advice or a guarantee. You decide '
               'what, if anything, to trade.')

    with st.form('add_position_form', clear_on_submit=True):
        st.subheader('Add a position')
        fc1, fc2 = st.columns(2)
        chain_key = fc1.selectbox('Chain', options=list(CHAINS.keys()), format_func=lambda k: CHAINS[k]['label'])
        symbol_in = fc2.text_input('Symbol (optional label)')
        token_in = st.text_input('Token contract address / mint')
        fc3, fc4 = st.columns(2)
        entry_price_in = fc3.number_input('Entry price (USD)', min_value=0.0, value=0.0, format='%f')
        amount_in = fc4.number_input('Amount held (tokens)', min_value=0.0, value=0.0)
        submitted = st.form_submit_button('Add position')
        if submitted:
            if not token_in or entry_price_in <= 0:
                st.error('Token address and a nonzero entry price are required.')
            else:
                cdb = conn()
                add_position(cdb, chain_key, token_in.strip(), symbol_in.strip() or '?', entry_price_in, amount_in, current_ladder)
                cdb.close()
                st.success('Position added.')

    st.divider()
    st.subheader('Open positions')
    if st.button('🔄 Refresh positions'):
        cdb = conn()
        with st.spinner('Refreshing...'):
            st.session_state.pos_updates = refresh_positions(cdb, current_ladder)
            st.session_state.pos_warnings = list(SCAN_WARNINGS)
        cdb.close()

    updates = st.session_state.get('pos_updates', [])
    st.session_state.open_tokens = updates
    pw = st.session_state.get('pos_warnings', [])
    if pw:
        with st.expander(f"⚠️ {len(pw)} data call(s) failed while refreshing (retried, then skipped)"):
            for w in pw[:50]:
                st.write('- ' + w)
    if not updates:
        st.info('No open positions yet, or you haven\'t refreshed since adding one.')
    else:
        for u in updates:
            if 'error' in u:
                st.error(f"{CHAINS[u['chain']]['label']} {u.get('symbol','?')}: {u['error']}")
                continue
            ath_flag = ' | 🚀 NEW ATH' if u.get('is_new_ath') else ''
            header = f"[{CHAINS[u['chain']]['label']}] {u['symbol']} — {u['multiple']:.2f}x | {u['remaining_pct']:.0f}% remaining | Safety {u['safety']['verdict']} | Exit Risk {u['dist_score']}{ath_flag}"
            with st.expander(header):
                if u.get('is_new_ath'):
                    st.success(f"🚀 **New all-time high (as far as we can tell)** — current MC ${u['mc']:,.0f} is at or above the highest point we know about (${u['best_known_peak']:,.0f}, from {u['ath_source']}). Worth deciding now whether to trim per your ladder below.")
                elif not u.get('true_ath_available'):
                    st.caption("True historical high isn't available for this chain (Robinhood Chain isn't covered by our history source) — ATH tracking here is limited to what we've personally observed since adding this position.")
                sv = u['safety']['verdict']
                safety_line = f"**Safety ({u['safety']['source']}):** " + (' • '.join(u['safety']['reasons']))
                if sv == 'DANGER':
                    st.error(safety_line + ' — this position is now flagged as a likely honeypot/rug. The refresh does not sell it for you.')
                elif sv == 'CAUTION':
                    st.warning(safety_line)
                elif sv == 'UNKNOWN':
                    st.info(safety_line + ' — treat as unverified.')
                else:
                    st.success(safety_line)
                st.write(f"**Entry:** ${u['entry_price']:.8g} → **Now:** ${u['price']:.8g}  |  **MC:** ${u['mc']:,.0f}  |  **Liquidity:** ${u['liq']:,.0f}")
                st.caption(f"Best-known all-time-high MC: ${u['best_known_peak']:,.0f} (source: {u['ath_source']})")
                if u['dist_score'] >= 40:
                    st.warning('**Exit-risk signal:** ' + (' • '.join(u['dist_why']) or 'elevated'))
                else:
                    st.write('**Exit risk:** ' + (' • '.join(u['dist_why']) or 'no warning signs yet'))
                if u['due_tiers']:
                    for (tier_m, tier_pct) in u['due_tiers']:
                        cA, cB = st.columns([3, 1])
                        cA.warning(f"Take-profit tier hit: **{tier_m}x** reached — ladder says trim **{tier_pct:.0f}%** of original stack.")
                        if cB.button('Mark sold', key=f"sell_{u['id']}_{tier_m}"):
                            cdb = conn()
                            mark_tier_sold(cdb, u['id'], tier_m, u['ladder'])
                            cdb.close()
                            st.rerun()
                else:
                    next_tiers = [t for t in u['ladder'] if t[0] not in u['triggered']]
                    if next_tiers:
                        nm, npct = next_tiers[0]
                        st.write(f"Next tier: **{nm}x** → trim **{npct:.0f}%** (currently at {u['multiple']:.2f}x)")
                    else:
                        st.write('All ladder tiers already triggered — riding the remainder.')
                if u['url']:
                    st.link_button('Open DexScreener', u['url'])
                if st.button('Close position', key=f"close_{u['id']}"):
                    cdb = conn()
                    close_position(cdb, u['id'])
                    cdb.close()
                    st.rerun()
