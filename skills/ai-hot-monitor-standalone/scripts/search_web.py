#!/usr/bin/env python3
import argparse, json, time, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from html.parser import HTMLParser

USER_AGENT = 'AIHotMonitorStandalone/1.0'

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]
    def handle_data(self,data): self.parts.append(data)
    def text(self): return ' '.join(''.join(self.parts).split())

class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.results=[]; self.current=None; self.capture=''
    def handle_starttag(self,tag,attrs):
        a=dict(attrs); c=set((a.get('class') or '').split())
        if 'result__a' in c:
            self._finish(); self.current={'title':'','url':a.get('href') or '','summary':''}; self.capture='title'
        elif self.current is not None and 'result__snippet' in c: self.capture='summary'
    def handle_data(self,data):
        if self.current is not None and self.capture: self.current[self.capture]+=data+' '
    def handle_endtag(self,tag):
        if tag=='a' and self.capture=='title': self.capture=''
    def _finish(self):
        if self.current and self.current['title'].strip(): self.results.append(self.current)
        self.current=None; self.capture=''
    def close(self):
        super().close(); self._finish()

def clean_html(v):
    p=TextExtractor(); p.feed(v or ''); return p.text()

def fetch(url,timeout=15):
    req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT})
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.read(1000000).decode('utf-8',errors='replace')

def google_news(query,region):
    params=urllib.parse.urlencode({'q':query,'hl':'zh-CN' if region=='china' else 'en','gl':'CN' if region=='china' else 'US','ceid':'CN:zh-Hans' if region=='china' else 'US:en'})
    try: payload=fetch('https://news.google.com/rss/search?'+params)
    except Exception: return []
    try: root=ET.fromstring(payload)
    except ET.ParseError: return []
    out=[]
    for item in root.findall('.//item'):
        title=(item.findtext('title') or '').strip(); link=(item.findtext('link') or '').strip(); desc=clean_html(item.findtext('description') or '')
        src=item.find('source'); pub=src.text.strip() if src is not None and src.text else ''
        if title and link:
            out.append({'platform':'Google News RSS','region':region,'language':'zh' if region=='china' else 'en','title':title,'summary':desc,'url':link,'source':pub or 'Google News','publisher':pub or 'Google News','published_at':(item.findtext('pubDate') or '').strip()})
    return out

def bing_rss(query):
    try: payload=fetch('https://www.bing.com/search?'+urllib.parse.urlencode({'q':query,'format':'rss'}))
    except Exception: return []
    try: root=ET.fromstring(payload)
    except ET.ParseError: return []
    out=[]
    for item in root.findall('.//item'):
        title=(item.findtext('title') or '').strip(); link=(item.findtext('link') or '').strip(); desc=clean_html(item.findtext('description') or '')
        if title and link:
            out.append({'platform':'Bing RSS','region':'global','language':'en','title':title,'summary':desc,'url':link,'source':'Bing','publisher':urllib.parse.urlparse(link).netloc,'published_at':(item.findtext('pubDate') or '').strip()})
    return out

def duckduckgo(query,region):
    try: payload=fetch('https://duckduckgo.com/?'+urllib.parse.urlencode({'q':query}))
    except Exception: return []
    p=DDGParser(); p.feed(payload); p.close()
    out=[]
    for it in p.results:
        title=it['title'].strip(); url=it['url'].strip()
        if 'uddg=' in url:
            q=urllib.parse.parse_qs(urllib.parse.urlparse(url).query); url=q.get('uddg',[url])[0]
        if title and url:
            out.append({'platform':'DuckDuckGo','region':region,'language':'zh' if region=='china' else 'en','title':title,'summary':' '.join(it['summary'].split()),'url':url,'source':'DuckDuckGo','publisher':urllib.parse.urlparse(url).netloc,'published_at':''})
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--query',required=True); ap.add_argument('--limit',type=int,default=10); ap.add_argument('--region',choices=['china','global','all'],default='all'); ap.add_argument('--output')
    a=ap.parse_args()
    combined=[]; regions=['china','global'] if a.region=='all' else [a.region]
    for region in regions:
        combined.extend(google_news(a.query,region)); combined.extend(duckduckgo(a.query,region)); time.sleep(0.5)
    combined.extend(bing_rss(a.query))
    seen=set(); unique=[]
    for item in combined:
        if item['url'] in seen: continue
        seen.add(item['url']); unique.append(item)
    unique=unique[:a.limit]
    if a.output:
        with open(a.output,'w',encoding='utf-8') as f: json.dump(unique,f,ensure_ascii=False,indent=2)
    else: print(json.dumps(unique,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
