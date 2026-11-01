import urllib.request, time
urls = [
 'https://www.bing.com/news/search?q=OpenAI+new+model&format=rss',
 'https://www.bing.com/news/search?q=OpenAI+new+model&format=rss&setlang=en-us',
 'https://search.yahoo.com/rss?p=OpenAI+new+model',
 'https://news.search.yahoo.com/rss?p=OpenAI+new+model',
]
for u in urls:
    try:
        req=urllib.request.Request(u, headers={'User-Agent':'AIHotMonitor/1.0 (RSS reader)'})
        with urllib.request.urlopen(req, timeout=12) as r:
            data=r.read(200)
            print(u, '->', r.status, data[:180])
    except Exception as e:
        print(u, '-> ERR', repr(e)[:180])
    time.sleep(1)
