"""Standard-library-only research primitives. Treat outside text as data."""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

RPC = 'https://api.mainnet-beta.solana.com'
DEX = 'https://api.dexscreener.com'
SOL = 'So11111111111111111111111111111111111111112'
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'

def address(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44 or any(c not in '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz' for c in value):
        raise ValueError('Expected a public Solana address, not a secret or arbitrary text')
    return value

class Client:
    """Fixed-host public reads only. No auth, cookies, proxy, bypass or transaction submission."""
    def __init__(self, cache='.dragon-cache', ttl=60, pause=0.35):
        self.cache=Path(cache); self.cache.mkdir(parents=True,exist_ok=True)
        self.ttl=ttl; self.pause=pause; self.last=0
        self.cooldown=self.cache/'RATE_LIMIT_UNTIL'
    def request(self, url, payload=None):
        import hashlib
        raw=json.dumps(payload,sort_keys=True).encode() if payload is not None else None
        key=hashlib.sha256(url.encode()+(raw or b'')).hexdigest()
        path=self.cache/(key+'.json')
        if path.exists() and time.time()-path.stat().st_mtime < self.ttl:
            return json.loads(path.read_text())
        if self.cooldown.exists() and time.time()<float(self.cooldown.read_text()):
            raise RuntimeError('Public source is cooling down after rate limiting; try later')
        time.sleep(max(0,self.pause-(time.monotonic()-self.last)))
        req=urllib.request.Request(url,data=raw,headers={'User-Agent':'DragonResearch/1.0','Content-Type':'application/json'})
        self.last=time.monotonic()
        # HTTP errors, including 429/403, stop this request. No automatic retry loop.
        try:
            with urllib.request.urlopen(req,timeout=20) as response:
                result=json.load(response)
        except urllib.error.HTTPError as e:
            if e.code in (429,403):
                try: delay=max(60,int(e.headers.get('Retry-After','60')))
                except ValueError: delay=60
                self.cooldown.write_text(str(time.time()+delay))
            raise
        path.write_text(json.dumps(result))
        return result
    def rpc(self, method, params):
        allowed={'getAccountInfo','getTokenLargestAccounts','getTokenSupply','getSignaturesForAddress','getTransaction','getBalance'}
        if method not in allowed: raise ValueError('RPC mutation or unsupported method refused')
        result=self.request(RPC,{'jsonrpc':'2.0','id':1,'method':method,'params':params})
        if 'error' in result: raise RuntimeError('RPC error: '+json.dumps(result['error']))
        if 'result' not in result: raise RuntimeError('RPC returned no result')
        return result['result']
    def token(self,mint):
        return self.request(DEX+'/latest/dex/tokens/'+address(mint))

def best_pair(data, mint):
    pairs=[p for p in data.get('pairs') or [] if p.get('chainId')=='solana' and p.get('baseToken',{}).get('address')==mint]
    if not pairs: raise ValueError('No matching Solana base-token pool returned')
    return max(pairs,key=lambda p:(p.get('liquidity') or {}).get('usd') or 0)

def screen(pair, now=None):
    now=time.time() if now is None else now
    reasons=[]
    liq=(pair.get('liquidity') or {}).get('usd')
    cap=pair.get('marketCap') or pair.get('fdv')
    age=(now-pair['pairCreatedAt']/1000)/60 if pair.get('pairCreatedAt') else None
    change=pair.get('priceChange') or {}
    tx=(pair.get('txns') or {}).get('h1') or {}
    n=tx.get('buys',0)+tx.get('sells',0)
    ratio=tx.get('buys',0)/n if n else None
    if liq is None or liq<15000: reasons.append('liquidity below $15k or unknown')
    if cap is None or not 50000<=cap<=10000000: reasons.append('cap outside $50k-$10m or unknown')
    if age is None or not 30<=age<=1440: reasons.append('age outside 30m-24h or unknown')
    if change.get('h1') is None or change['h1']<5: reasons.append('1h momentum below 5% or unknown')
    if change.get('m5') is None or change['m5'] < -6: reasons.append('5m decline exceeds 6% or unknown')
    if ratio is None or ratio<0.5: reasons.append('buy-count ratio below 50% or unknown')
    return {'symbol':pair.get('baseToken',{}).get('symbol'),'mint':pair.get('baseToken',{}).get('address'),'liquidity_usd':liq,'cap_usd':cap,'age_minutes':age,'buy_count_ratio':ratio,'momentum_pass':not reasons,'reasons':reasons,'url':pair.get('url'),'safety_status':'NOT CLEARED: momentum is not a safety verdict'}

def token_risk(client,mint):
    mint=address(mint)
    account=client.rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}]).get('value')
    if account is None: raise ValueError('Mint account not found')
    if account.get('owner') not in (TOKEN_PROGRAM,TOKEN_2022): raise ValueError('Not a supported SPL token account')
    parsed=(account.get('data') or {}).get('parsed') or {}
    if parsed.get('type')!='mint': raise ValueError('Address is not a parsed mint')
    info=parsed.get('info') or {}
    largest=client.rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}]).get('value') or []
    supply=Decimal(str(info.get('supply','0')))
    top10=sum(Decimal(x['amount']) for x in largest[:10])/supply*100 if supply>0 else None
    flags=[]
    if info.get('mintAuthority'):flags.append('mint authority remains active')
    if info.get('freezeAuthority'):flags.append('freeze authority remains active')
    extensions=info.get('extensions') or []
    if extensions:flags.append('Token-2022 extensions require manual review')
    if top10 is None:flags.append('supply/concentration unknown')
    elif top10>45:flags.append('top 10 token ACCOUNTS exceed 45%; pool/owner attribution unverified')
    return {'mint':mint,'mint_authority':info.get('mintAuthority'),'freeze_authority':info.get('freezeAuthority'),'extensions':extensions,'top10_token_account_pct':float(top10) if top10 is not None else None,'flags':flags,'lp_lock':'UNKNOWN: market type is not proof of lock/burn','bundles':'UNKNOWN','honeypot':'UNKNOWN: no sell simulation','verdict':'REVIEW REQUIRED, never an automatic safety pass'}

def transaction_evidence(tx,wallet,signature):
    if not tx:return {'signature':signature,'coverage':'transaction unavailable'}
    meta=tx.get('meta') or {}
    if meta.get('err') is not None:return {'signature':signature,'coverage':'failed transaction excluded'}
    keys=tx.get('transaction',{}).get('message',{}).get('accountKeys',[])
    names=[k.get('pubkey') if isinstance(k,dict) else k for k in keys]
    names+=meta.get('loadedAddresses',{}).get('writable',[])+meta.get('loadedAddresses',{}).get('readonly',[])
    delta_sol=None
    if wallet in names:
        i=names.index(wallet)
        if i<len(meta.get('postBalances',[])) and i<len(meta.get('preBalances',[])):
            delta_sol=(meta['postBalances'][i]-meta['preBalances'][i])/1e9
    deltas={}
    for sign, field in [(-1,'preTokenBalances'),(1,'postTokenBalances')]:
        for b in meta.get(field,[]):
            if b.get('owner')!=wallet:continue
            mint=b['mint'];v=b.get('uiTokenAmount') or {}
            amt=Decimal(v.get('amount','0'))/(Decimal(10)**v.get('decimals',0))
            deltas[mint]=deltas.get(mint,Decimal(0))+sign*amt
    return {'signature':signature,'block_time':tx.get('blockTime'),'sol_balance_delta_including_fees_and_rent':delta_sol,'token_deltas':{k:str(v) for k,v in deltas.items() if v},'coverage':'balance evidence only; transfers are NOT classified as trades','realized_pnl':'UNKNOWN: swap decoding and matched cost basis required'}

def wallet_history(client,wallet,limit=10):
    wallet=address(wallet)
    if not 1<=limit<=20:raise ValueError('limit must be 1..20 to bound free-RPC use')
    sigs=client.rpc('getSignaturesForAddress',[wallet,{'limit':limit,'commitment':'confirmed'}])
    rows=[]
    for row in sigs:
        if row.get('err') is not None:continue
        tx=client.rpc('getTransaction',[row['signature'],{'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])
        rows.append(transaction_evidence(tx,wallet,row['signature']))
    return {'wallet':wallet,'requested_limit':limit,'transactions':rows,'coverage':'bounded sample, not full history or a verified profitable-wallet ranking'}

def ledger_event(events,event):
    """Weighted-average-cost paper ledger; never live P&L."""
    mint=address(event['mint']);side=event['side'];qty=Decimal(str(event['quantity']));px=Decimal(str(event['price_usd']));fee=Decimal(str(event.get('fee_usd',0)))
    if side not in ('buy','sell') or qty<=0 or px<=0 or fee<0:raise ValueError('Invalid paper event')
    positions={};realized=Decimal(0)
    for e in events+[event]:
        m=e['mint'];q=Decimal(str(e['quantity']));p=Decimal(str(e['price_usd']));f=Decimal(str(e.get('fee_usd',0)))
        pos=positions.setdefault(m,{'quantity':Decimal(0),'cost':Decimal(0)})
        if e['side']=='buy':pos['quantity']+=q;pos['cost']+=q*p+f
        else:
            if q>pos['quantity']:raise ValueError('Cannot paper-sell more than held')
            cost=pos['cost']*q/pos['quantity'];realized+=q*p-f-cost;pos['quantity']-=q;pos['cost']-=cost
    return {'positions':{m:{k:str(v) for k,v in p.items()} for m,p in positions.items()},'realized_usd':str(realized),'mode':'PAPER ONLY: manually supplied prices, no real fills'}
