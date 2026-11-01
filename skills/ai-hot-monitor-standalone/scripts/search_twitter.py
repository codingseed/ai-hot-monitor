#!/usr/bin/env python3
import argparse, json, os, time, urllib.parse, urllib.request

API_KEY=os.environ.get('TWITTERAPI_API_KEY','').strip()
BASE='https://api.twitterapi.io/twitter/tweet/advanced_search'
USER_AGENT='AIHotMonitorStandalone/1.0'

def api_get(params):
    url=BASE+'?'+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={'X-API-Key':API_KEY,'User-Agent':USER_AGENT})
    with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read(1000000))

def normalize(tweet):
    author=tweet.get('author') or {}
    return {
      'platform':'twitterapi.io','region':'global','language':'en',
      'title':(tweet.get('text') or '')[:300],
      'summary':(tweet.get('text') or '')[:1600],
      'url':tweet.get('url') or '',
      'source':'X @'+str(author.get('userName') or ''),
      'publisher':author.get('userName') or '',
      'published_at':tweet.get('createdAt') or '',
      'external_id':str(tweet.get('id') or ''),
      'like_count':int(tweet.get('likeCount') or 0),
      'retweet_count':int(tweet.get('retweetCount') or 0),
      'reply_count':int(tweet.get('replyCount') or 0),
      'view_count':int(tweet.get('viewCount') or 0),
      'author_followers':int((author.get('followers') if isinstance(author.get('followers'),int) else author.get('followersCount')) or 0),
      'author_verified':bool(author.get('isBlueVerified')), 
    }

def eligible(item):
    return item['like_count']>=10 and item['retweet_count']>=5 and item['view_count']>=500 and item['author_followers']>=100

def search(query,limit):
    if not API_KEY: return [],['TWITTERAPI_API_KEY is not set; X search skipped']
    results=[]; errors=[]
    for sort in ('Top','Latest'):
        cursor=''; pages=0
        while pages<2 and len(results)<limit:
            params={'query':query,'queryType':sort}
            if cursor: params['cursor']=cursor
            try:
                data=api_get(params); pages+=1
            except Exception as exc:
                errors.append(f'X {sort} failed: {str(exc)[:120]}'); break
            for tweet in data.get('tweets') or []:
                item=normalize(tweet)
                if item not in results: results.append(item)
            cursor=data.get('next_cursor') or ''
            if not data.get('has_next_page'): break
            time.sleep(5)
    # Apply eligibility gate
    filtered=[item for item in results if eligible(item)]
    return filtered[:limit], errors

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--query',required=True); ap.add_argument('--limit',type=int,default=10); ap.add_argument('--output')
    a=ap.parse_args()
    items,errors=search(a.query,a.limit)
    payload={'results':items,'errors':errors}
    if a.output:
        with open(a.output,'w',encoding='utf-8') as f: json.dump(payload,f,ensure_ascii=False,indent=2)
    else: print(json.dumps(payload,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
