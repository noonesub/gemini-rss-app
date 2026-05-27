import json, os, urllib.request

with open('x_data/tweets.json', encoding='utf-8') as f:
    tweets = json.load(f)

target = None
for t in tweets:
    if t.get('images'):
        target = t
        break

if not target:
    print('没有找到带图片的推文')
    exit(1)

out_dir = 'x_data/downloads'
os.makedirs(out_dir, exist_ok=True)

print(f'推文: {target["url"]}')
print(f'内容: {target["content"][:80]}')

for i, url in enumerate(target['images']):
    hq_url = url.replace('?name=small', '?name=orig')
    filename = f'{target["id"]}_{i}.jpg'
    filepath = os.path.join(out_dir, filename)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': target['url'],
    }
    req = urllib.request.Request(hq_url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        with open(filepath, 'wb') as f:
            f.write(resp.read())
    print(f'已下载: {filepath}')
