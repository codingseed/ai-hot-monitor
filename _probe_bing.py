import urllib.request, urllib.parse
u='https://www.bing.com/search?q=OpenAI+new+model&format=rss&count=5'
req=urllib.request.Request(u, headers={'User-Agent':'AIHotMonitor/1.0 (RSS reader)'})
with urllib.request.urlopen(req, timeout=15) as r:
    data=r.read()
text=data.decode('utf-8', errors='replace')[:5000]
with open('e:/proj/ai-hot-monitor/_bing_out.txt','w',encoding='utf-8') as f:
    f.write(text)
print('done')
