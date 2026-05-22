#!/usr/bin/env python3
import json, os, time
from pathlib import Path
from urllib.parse import urlparse
import requests, tldextract
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import RatelimitException

KEYWORDS_FILE=Path('keywords.txt'); NEGATIVE_KEYWORDS_FILE=Path('negative_keywords.txt')
PROGRESS_FILE=Path('serp_progress.txt'); SEEDS_PARTIAL_FILE=Path('seeds_partial.txt'); SEEDS_FILE=Path('seeds.txt'); SEED_SOURCE_MAP_FILE=Path('seed_sources.json')
KNOWN_SITE_NAMES={"zillow","redfin","trulia","realtor","homes","opendoor","offerpad","homevestors","homelight","movoto","loopnet","biggerpockets","youtube","facebook","instagram","twitter","tiktok","linkedin","yelp","wikipedia","reddit","quora","pinterest","nextdoor","craigslist","angieslist","thumbtack","homeadvisor","angi","apartmentlist","apartments","bankofamerica","wellsfargo","chase","google","bing","yahoo","msn","hud","gov"}
HARDCODED_EXCLUSIONS={"zillow","redfin","trulia","realtor","homes.com","opendoor","offerpad","homevestors","homelight","movoto","loopnet","biggerpockets","foreclosure","auction","bankowned","fanniemae","freddiemac","youtube","facebook","instagram","twitter","tiktok","linkedin","yelp","bbb.org","wikipedia","reddit","quora","pinterest","nextdoor","craigslist","angieslist","thumbtack","homeadvisor","angi","apartmentlist","apartments","rent","hud","gov",".edu","bankofamerica","wellsfargo","chase","google","bing","yahoo","msn"}
CARROT_URLS=["https://carrot.com/","https://carrot.com/case-studies/","https://carrot.com/reviews/"]
FB_TERMS=["we buy houses","sell my house fast","cash home buyers","we buy ugly houses","cash for your house","house buyers","sell house for cash","cash offer for home","home investors","we buy homes","sell my house as is"]

def normalize_domain(v:str)->str|None:
    c=(v or '').strip().lower()
    if not c:return None
    if '://' not in c:c='http://'+c
    p=urlparse(c); host=p.netloc or p.path.split('/')[0]
    d=tldextract.extract(host).top_domain_under_public_suffix
    return d.lower() if d else None

def save_domains(path:Path,domains:set[str]): path.write_text('\n'.join(sorted(domains))+'\n',encoding='utf-8')
def load_completed()->set[str]: return set(PROGRESS_FILE.read_text(encoding='utf-8').splitlines()) if PROGRESS_FILE.exists() else set()
def excluded(domain:str,terms:set[str])->bool: return any(t in domain for t in terms)

def load_exclusions()->set[str]:
    ex={x.lower() for x in HARDCODED_EXCLUSIONS}
    for line in NEGATIVE_KEYWORDS_FILE.read_text(encoding='utf-8').splitlines():
        t=line.strip().lower()
        if t and ('.' in t or t in KNOWN_SITE_NAMES): ex.add(t)
    return ex

def scrape_serp(keywords:list[str]):
    completed=load_completed(); domains=set(); done=len(completed)
    with PROGRESS_FILE.open('a',encoding='utf-8') as pf, DDGS() as ddgs:
        try:
            for kw in keywords:
                if kw in completed: continue
                success=False
                for attempt in range(2):
                    try:
                        for r in ddgs.text(kw,max_results=8):
                            d=normalize_domain((r.get('href') or r.get('url') or ''))
                            if d: domains.add(d)
                        pf.write(kw+'\n'); pf.flush(); done+=1; success=True; break
                    except RatelimitException:
                        print(f"[WARN] DuckDuckGo rate limited for query: {kw}")
                        if attempt==0: time.sleep(60); continue
                        print(f"[WARN] Skipping after retry due to rate limit: {kw}")
                    except Exception as exc:
                        print(f"[WARN] Query failed ({kw}): {exc}"); break
                if done%50==0 and done>0: print(f"SERP: {done}/{len(keywords)} done, {len(domains)} unique domains so far")
                if done%200==0 and done>0: save_domains(SEEDS_PARTIAL_FILE,domains)
                if success: time.sleep(3)
        except KeyboardInterrupt:
            save_domains(SEEDS_PARTIAL_FILE,domains); raise
    return domains,done

def scrape_carrot():
    out=set()
    for url in CARROT_URLS:
        try:
            r=requests.get(url,timeout=20)
            if r.status_code!=200: continue
            for a in BeautifulSoup(r.text,'html.parser').select('a[href]'):
                d=normalize_domain(a.get('href',''))
                if d and d!='carrot.com': out.add(d)
            time.sleep(2)
        except Exception: pass
    return out

def scrape_fb():
    tok=os.getenv('FB_ACCESS_TOKEN','').strip()
    if not tok: print('[INFO] Facebook source skipped: FB_ACCESS_TOKEN missing.'); return set(),True
    out=set(); endpoint='https://graph.facebook.com/v20.0/ads_archive'
    for term in FB_TERMS:
        try:
            r=requests.get(endpoint,params={"access_token":tok,"search_terms":term,"ad_type":"ALL","ad_reached_countries":'["US"]',"ad_active_status":"ACTIVE","fields":"page_name,ad_creative_link_url","limit":500},timeout=30)
            if r.status_code in (400,401): print(f"[ERROR] Facebook source skipped due to token/API error ({r.status_code})."); return set(),True
            r.raise_for_status()
            for item in r.json().get('data',[]):
                d=normalize_domain(item.get('ad_creative_link_url',''))
                if d: out.add(d)
            time.sleep(3)
        except Exception as exc:
            print(f"[WARN] Facebook term failed ({term}): {exc}"); time.sleep(3)
    return out,False

def main():
    keywords=[k.strip() for k in KEYWORDS_FILE.read_text(encoding='utf-8').splitlines() if k.strip()]
    exclusions=load_exclusions()
    try: a,done=scrape_serp(keywords)
    except KeyboardInterrupt:
        partial=scrape_carrot()
        if partial: save_domains(SEEDS_PARTIAL_FILE,partial)
        print('Interrupted. Progress saved. Re-run to resume.'); return
    b=scrape_carrot(); c,fb_skipped=scrape_fb()
    combined=list(a)+list(b)+list(c); unique={d.lower() for d in combined}; final=sorted(d for d in unique if not excluded(d,exclusions))
    source_map={}
    for d in a: source_map.setdefault(d,set()).add('SERP')
    for d in b: source_map.setdefault(d,set()).add('Carrot_showcase')
    for d in c: source_map.setdefault(d,set()).add('Facebook')
    save_domains(SEEDS_FILE,set(final))
    SEED_SOURCE_MAP_FILE.write_text(json.dumps({d:'+'.join(sorted(source_map.get(d,{'SERP'}))) for d in final},indent=2,sort_keys=True),encoding='utf-8')
    print('HARVEST COMPLETE')
    print(f'Source A (SERP):     {len(a)} domains from {done} queries')
    print(f'Source B (Carrot):   {len(b)} domains')
    print(f"Source C (Facebook): {'skipped' if fb_skipped else len(c)} domains")
    print(f'Before dedup:        {len(combined)} total')
    print(f'After dedup:         {len(unique)} unique')
    print(f'After exclusions:    {len(final)} final')
    print('Saved to seeds.txt')

if __name__=='__main__': main()
