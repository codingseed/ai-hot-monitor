#!/usr/bin/env python3
import argparse, json, os

def load(path):
    with open(path,encoding='utf-8') as f: data=json.load(f)
    if isinstance(data,list): return data
    if isinstance(data,dict):
        if isinstance(data.get('results'),list): return data['results']
        # allow {items:[]}
        for key in ('items','stories'):
            if isinstance(data.get(key),list): return data[key]
    return []

def dedup(items):
    seen=set(); out=[]
    for item in items:
        key=item.get('url') or item.get('title') or ''
        if key in seen: continue
        seen.add(key); out.append(item)
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--inputs',nargs='+',required=True); ap.add_argument('--output')
    a=ap.parse_args()
    all_items=[]
    for path in a.inputs:
        all_items.extend(load(path))
    unique=dedup(all_items)
    lines=['# AI Hotspot Report','','## Candidate Count',f'- Raw: {len(all_items)}',f'- Unique: {len(unique)}','','## Candidates','']
    for i,item in enumerate(unique,1):
        lines.append(f'### {i}. {item.get("title","")}')
        lines.append(f'- Platform: {item.get("platform","")} | Region: {item.get("region","")} | Language: {item.get("language","")}')
        lines.append(f'- Publisher: {item.get("publisher") or item.get("source","")}')
        lines.append(f'- Published: {item.get("published_at","")}')
        lines.append(f'- URL: {item.get("url","")}')
        if item.get('summary'): lines.append(f'- Summary: {item.get("summary","")}')
        lines.append('')
    lines.extend(['## Analysis Required','Claude should assign relevance, urgency, authenticity, summary, and rationale using references/.',''])
    text='\n'.join(lines)
    if a.output:
        with open(a.output,'w',encoding='utf-8') as f: f.write(text)
    else: print(text)

if __name__=='__main__': main()
