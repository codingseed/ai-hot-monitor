import urllib.request, urllib.parse
tests = [
  'https://www.bing.com/news/search?q=OpenAI+new+model&format=rss',
  'https://www.bing.com/search?q=OpenAI+new+model&format=rss',
  'https://news.baidu.com/ns?word=OpenAI+new+model&tn=news',
]
for u in tests:
    try:
        req=urllib.request.Request(u, headers={'User-Agent':'AIHotMonitor/1.0 (RSS reader)'})
        with urllib.request.urlopen(req, timeout=15) as r:
            data=r.read(500)
            print(u, '->', r.status, data[:200])
    except Exception as e:
        print(u, '-> ERR', repr(e)[:200])
