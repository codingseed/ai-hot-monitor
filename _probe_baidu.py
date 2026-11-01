import urllib.request, urllib.parse, time
urls = [
 'https://www.baidu.com/s?wd=OpenAI+new+model',
 'https://news.baidu.com/ns?word=OpenAI+new+model',
]
for u in urls:
    try:
        req=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=12) as r:
            data=r.read(500)
            print(u, '->', r.status, data[:300])
    except Exception as e:
        print(u, '-> ERR', repr(e)[:200])
    time.sleep(1)
