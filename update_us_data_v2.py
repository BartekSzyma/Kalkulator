#!/usr/bin/env python3
from __future__ import annotations
import io,json,time
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
import pandas as pd, requests

HERE=Path(__file__).resolve().parent
MAP=json.loads((HERE/'us_company_map.json').read_text(encoding='utf-8'))
OUT=HERE/'data'/'us_companies.json'
UA='M3S valuation research contact: BartekSzyma@users.noreply.github.com'
S=requests.Session(); S.headers.update({'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})

def get_json(url):
    r=S.get(url,headers={'User-Agent':UA,'Accept-Encoding':'gzip, deflate'},timeout=40); r.raise_for_status(); return r.json()
def get_text(url):
    r=S.get(url,timeout=40); r.raise_for_status(); return r.text

def units(cf,concept,ns='us-gaap'):
    f=cf.get('facts',{}).get(ns,{}).get(concept,{}).get('units',{})
    for k in ('USD','shares'):
        if k in f:return f[k]
    return []
def choose_units(cf,concepts,ns='us-gaap'):
    for c in concepts:
        u=units(cf,c,ns)
        if u:return c,u
    return None,[]
def instant(cf,concepts,ns='us-gaap'):
    tag,u=choose_units(cf,concepts,ns); out=defaultdict(list)
    for x in u:
        if x.get('form') not in {'10-Q','10-K','10-Q/A','10-K/A'} or x.get('fy') is None or x.get('fp') not in {'Q1','Q2','Q3','FY'}:continue
        q={'Q1':1,'Q2':2,'Q3':3,'FY':4}[x['fp']]; out[f"{int(x['fy'])}/Q{q}"].append(x)
    res={}
    for p,rows in out.items(): rows.sort(key=lambda z:z.get('filed','')); res[p]=rows[-1].get('val')
    return res,tag
def flow(cf,concepts):
    tag,u=choose_units(cf,concepts); by=defaultdict(list)
    for x in u:
        if x.get('form') not in {'10-Q','10-K','10-Q/A','10-K/A'} or x.get('fy') is None or x.get('fp') not in {'Q1','Q2','Q3','FY'} or not x.get('start') or not x.get('end'):continue
        d=(pd.Timestamp(x['end'])-pd.Timestamp(x['start'])).days+1
        if 60<=d<=430: by[(int(x['fy']),x['fp'])].append((abs(d-{'Q1':91,'Q2':182,'Q3':273,'FY':365}[x['fp']]),x.get('filed',''),x.get('val')))
    sel={}
    for k,rows in by.items(): rows.sort(key=lambda z:(z[0],z[1])); best=rows[0][0]; cand=[r for r in rows if r[0]<=best+10]; cand.sort(key=lambda z:z[1]); sel[k]=cand[-1][2]
    out={}
    for fy in sorted({k[0] for k in sel}):
        q1=sel.get((fy,'Q1'));q2=sel.get((fy,'Q2'));q3=sel.get((fy,'Q3'));fyv=sel.get((fy,'FY'))
        if q1 is not None: out[f'{fy}/Q1']=q1
        if q2 is not None: out[f'{fy}/Q2']=q2-(q1 or 0)
        if q3 is not None: out[f'{fy}/Q3']=q3-(q2 or 0)
        if fyv is not None: out[f'{fy}/Q4']=fyv-(q3 or 0)
    return out,tag
def rolling4(m):
    ps=sorted(m,key=lambda p:(int(p.split('/Q')[0]),int(p.split('/Q')[1]))); out={}
    for i,p in enumerate(ps):
        vals=[m.get(x) for x in ps[i-3:i+1]] if i>=3 else []
        out[p]=sum(vals) if i>=3 and all(v is not None for v in vals) else None
    return out
def addmaps(*ms):
    ks=set().union(*(m.keys() for m in ms)); return {k:sum((m.get(k) or 0) for m in ms) for k in ks}
def prices(ticker):
    txt=get_text(f'https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d')
    df=pd.read_csv(io.StringIO(txt)); df['Date']=pd.to_datetime(df['Date']); return df.sort_values('Date'),float(df.iloc[-1]['Close'])
def pbefore(df,d):
    if not d:return None
    x=df[df.Date<=pd.Timestamp(d)]; return None if x.empty else float(x.iloc[-1].Close)
def ends(cf):
    _,u=choose_units(cf,['Assets']); out={}
    for x in u:
        if x.get('form') in {'10-Q','10-K','10-Q/A','10-K/A'} and x.get('fy') is not None and x.get('fp') in {'Q1','Q2','Q3','FY'} and x.get('end'):
            q={'Q1':1,'Q2':2,'Q3':3,'FY':4}[x['fp']]; out[f"{int(x['fy'])}/Q{q}"]=x['end']
    return out

def one(ticker,meta):
    cik=meta['cik']; cf=get_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json')
    eq,eqt=instant(cf,['StockholdersEquity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'])
    gw,gwt=instant(cf,['Goodwill']); cash,ct=instant(cf,['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'])
    sd1,sdt1=instant(cf,['LongTermDebtCurrent','CurrentPortionOfLongTermDebt']); sd2,sdt2=instant(cf,['ShortTermBorrowings']); ld,ldt=instant(cf,['LongTermDebtNoncurrent','LongTermDebtAndFinanceLeaseObligationsNoncurrent'])
    lc,lct=instant(cf,['OperatingLeaseLiabilityCurrent','FinanceLeaseLiabilityCurrent']); ln,lnt=instant(cf,['OperatingLeaseLiabilityNoncurrent','FinanceLeaseLiabilityNoncurrent'])
    sh,sht=instant(cf,['EntityCommonStockSharesOutstanding'],ns='dei')
    ni,nit=flow(cf,['NetIncomeLoss','ProfitLoss']); dv,dvt=flow(cf,['PaymentsOfDividendsCommonStock','PaymentsOfDividends']); bb,bbt=flow(cf,['PaymentsForRepurchaseOfCommonStock','PaymentsForRepurchaseOfEquity'])
    nttm, dttm, bttm=rolling4(ni),rolling4({k:abs(v) for k,v in dv.items()}),rolling4({k:abs(v) for k,v in bb.items()})
    pdf,latest=prices(ticker); pe=ends(cf); sd=addmaps(sd1,sd2); lease=addmaps(lc,ln)
    ps=sorted(set(eq)&set(nttm)&set(sh),key=lambda p:(int(p.split('/Q')[0]),int(p.split('/Q')[1]))); hist=[]
    for p in ps[-12:]:
        if eq.get(p) is None or nttm.get(p) is None or not sh.get(p):continue
        hist.append({'period':p,'equity':eq[p],'goodwill':gw.get(p) or 0,'net_profit_ttm':nttm[p],'dividend_ttm':dttm.get(p) or 0,'buyback_ttm':bttm.get(p) or 0,'cash':cash.get(p) or 0,'short_debt':sd.get(p) or 0,'long_debt':ld.get(p) or 0,'lease':lease.get(p) or 0,'shares':sh[p],'price':pbefore(pdf,pe.get(p)),'report_end':pe.get(p)})
    if not hist: raise RuntimeError('brak kompletnego okresu SEC z equity, TTM i shares')
    cur=dict(hist[-1]);cur['price']=latest
    warns=[]
    if not gw:warns.append('Brak osobnego tagu goodwill; przyjęto 0.')
    if not dv:warns.append('Brak standardowego tagu dywidend; przyjęto 0.')
    if not bb:warns.append('Brak standardowego tagu buybacku; przyjęto 0.')
    return {'ticker':ticker,'name':meta['name'],'cik':cik,'currency':'USD','current':cur,'history':hist[-8:],'warnings':warns,'tags':{'equity':eqt,'goodwill':gwt,'cash':ct,'short_debt':[sdt1,sdt2],'long_debt':ldt,'lease':[lct,lnt],'shares':sht,'net_income':nit,'dividends':dvt,'buyback':bbt},'sources':{'sec':f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json','prices':f'https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d'}}

def main():
    payload={'generated_at':datetime.now(timezone.utc).isoformat(),'market':'USA','companies':{},'errors':{}}
    for i,(t,m) in enumerate(MAP.items(),1):
        print(f'[{i}/{len(MAP)}] {t}',flush=True)
        try:payload['companies'][t]=one(t,m);print('  OK',flush=True)
        except Exception as e:payload['errors'][t]=str(e);print('  ERROR:',e,flush=True)
        time.sleep(.2)
    OUT.parent.mkdir(exist_ok=True);OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');print(f"Saved {len(payload['companies'])} USA companies, {len(payload['errors'])} errors")
if __name__=='__main__':main()
