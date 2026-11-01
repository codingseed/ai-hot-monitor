import urllib.request, urllib.parse, time
u='https://www.sogou.com/web?query=OpenAI+new+model'
req=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
with urllib.request.urlopen(req, timeout=12) as r:
    data=r.read(3000)
with open('e:/proj/ai-hot-monitor/_sogou_out.txt','wb') as f:
    f.write(data)
print('done', len(data))
