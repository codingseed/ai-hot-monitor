import json, urllib.request, urllib.parse, os
from dotenv import load_dotenv
load_dotenv('e:/proj/ai-hot-monitor/.env')
key=os.environ.get('TWITTERAPI_API_KEY')
print('key present:', bool(key))
params=urllib.parse.urlencode({'query':'AI coding min_faves:5 -filter:retweets','queryType':'Latest','cursor':''})
req=urllib.request.Request('https://api.twitterapi.io/twitter/tweet/advanced_search?'+params, headers={'X-API-Key':key,'User-Agent':'AIHotMonitor/1.0'})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        data=json.loads(r.read())
    print('top keys:', list(data.keys()))
    print('count:', len(data.get('tweets',[])))
    if data.get('tweets'):
        t=data['tweets'][0]
        print(json.dumps(t, ensure_ascii=False, indent=2)[:5000])
except Exception as e:
    print('ERR', repr(e))
