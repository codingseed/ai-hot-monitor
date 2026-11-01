#!/usr/bin/env python3
import argparse, json, time, urllib.parse, urllib.request

USER_AGENT='AIHotMonitorStandalone/1.0'
def fetch(url,timeout=15):
    req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT})
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.read(1000000).decode('utf-8',errors='replace')

def bilibili_search(query,limit):
    params=urllib.parse.urlencode({'search_type':'video','keyword':query,'page':1,'pagesize':min(limit,20)})
    url='https://api.bilibili.com/x/web-interface/search/type?'+params
    try: payload=fetch(url)
    except Exception as exc: return [], f'Bilibili search unavailable: {str(exc)[:120]}'
    try: data=__import__('json').loads(payload)
    except Exception: return [], 'Bilibili returned non-JSON'
    if data.get('code')!=0: return [], f'Bilibili API code {data.get("code")}: {data.get("message")}'
    out=[]
    for item in (data.get('data') or {}).get('result') or []:
        title=item.get('title','').replace('<em class="keyword">','').replace('</em>','')
        bvid=item.get('bvid') or item.get('aid')
        url=f'https://www.bilibili.com/video/{bvid}' if bvid else ''
        if title and url:
            out.append({'platform':'Bilibili','region':'china','language':'zh','title':title,'summary':item.get('description') or '','url':url,'source':'Bilibili','publisher':item.get('author') or 'Bilibili','published_at':''})
    return out[:limit], ''

def bing_china(query,limit):
    # Bing RSS has no stable China-only endpoint in this standalone version; use Google News China via search_web logic by importing? Keep simple: return []
    return [], 'Bing China RSS not separately implemented in standalone skill'

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--query',required=True); ap.add_argument('--limit',type=int,default=10); ap.add_argument('--output')
    a=ap.parse_args()
    combined=[]; errors=[]
    b_items,b_err=bilibili_search(a.query,a.limit); combined.extend(b_items)
    if b_err: errors.append(b_err)
    # Dedup
    seen=set(); unique=[]
    for item in combined:
        if item['url'] in seen: continue
        seen.add(item['url']); unique.append(item)
    unique=unique[:a.limit]
    payload={'results':unique,'errors':errors}
    if a.output:
        with open(a.output,'w',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False,indent=2)
    else: print(json.dumps(payload,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
