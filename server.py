"""Your own CricClubs -> overlay server. No third-party scoring API.
Run:  pip install requests beautifulsoup4 lxml cloudscraper && python server.py
Then open  http://localhost:8080/  to make your overlay link.
Endpoint: /match?link=<CricClubs scorecard URL>   (or ?league=LPCL&matchId=..&clubId=..)
"""
import json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
try:
    import cloudscraper; HTTP = cloudscraper.create_scraper()
except ImportError:
    import requests; HTTP = requests.Session()

HERE = os.path.dirname(os.path.abspath(__file__))
STATE, LOCK = {}, threading.Lock()
balls = lambda ov: (lambda a, b: int(a or 0) * 6 + int(b or 0))(*(str(ov).split('.') + ['0'])[:2])
num = lambda s: int(re.sub(r'\D', '', s) or 0)
txt = lambda e: re.sub(r'\s+', ' ', e.get_text(' ', strip=True))


def parse(html):
    s = BeautifulSoup(html, 'lxml')
    for x in s(['script', 'style']): x.decompose()
    teams = []
    for li in s.select('.score-top li'):
        n = li.select_one('.teamName')
        if n:
            sp = li.find_all('span'); sc = txt(sp[1]) if len(sp) > 1 else '0/0'
            ov = re.search(r'([\d.]+)\s*/\s*(\d+)', txt(li.find('p')) if li.find('p') else '')
            teams.append(dict(name=txt(n), score=sc, ov=ov.group(1) if ov else '0.0', max=int(ov.group(2)) if ov else 20))
    logos = [i['src'] for i in s.select('.vsteam-image img')]
    innings, tabs = [], s.select('table.table')
    i = 0
    while i < len(tabs):
        head = txt(tabs[i].find('thead')) if tabs[i].find('thead') else ''
        if 'innings' in head:
            bat = []; extras = dict(w=0, nb=0)
            for tr in tabs[i].find_all('tr')[1:]:
                c = [txt(x) for x in tr.find_all(['td', 'th'])]
                if not c: continue
                if c[0].startswith('Extras'):
                    m = dict(re.findall(r'(\w+)\s+(\d+)', c[0])); extras = dict(w=int(m.get('w', 0)), nb=int(m.get('nb', 0)))
                elif c[0].startswith('Total'): pass
                elif len(c) >= 7:
                    b = tr.find('b'); name = txt(b) if b else c[0]
                    bat.append(dict(name=name.rstrip('*'), r=num(c[2]), b=num(c[3]), f=num(c[4]), s=num(c[5]), no='not out' in c[1]))
            bowl = []
            j = i + 1
            while j < len(tabs) and 'innings' not in (txt(tabs[j].find('thead')) if tabs[j].find('thead') else ''):
                if txt(tabs[j]).startswith('Bowling'):
                    for tr in tabs[j].find_all('tr')[1:]:
                        c = [txt(x) for x in tr.find_all(['td', 'th'])]
                        if len(c) >= 7 and c[1]: bowl.append(dict(name=c[1], o=c[2], r=c[5], w=num(c[6])))
                    break
                j += 1
            innings.append(dict(bat=bat, bowl=bowl, extras=extras))
        i += 1
    status = s.select_one('.score-top .container > h3')
    return dict(teams=teams, logos=logos, innings=innings, status=txt(status) if status else '')


def live(key, p):
    """Turn a parsed page into the overlay JSON; ball-by-ball detail is derived from poll-to-poll changes."""
    n = len(p['innings']) or 1
    inn = p['innings'][n - 1] if p['innings'] else dict(bat=[], bowl=[], extras=dict(w=0, nb=0))
    bt = p['teams'][n - 1] if len(p['teams']) >= n else dict(name='', score='0/0', ov='0.0', max=20)
    ot = p['teams'][2 - n] if len(p['teams']) == 2 else dict(name='')
    runs, wk = (list(map(int, re.findall(r'\d+', bt['score']))) + [0, 0])[:2]
    at = [b for b in inn['bat'] if b['no']][:2] or inn['bat'][-2:]
    prev = STATE.get(key)
    st = dict(runs=runs, ov=bt['ov'], wk=wk, bat={b['name']: b for b in at}, bowl={b['name']: b['o'] for b in inn['bowl']},
              ex=inn['extras'], over=[], strike=at[0]['name'] if at else '', bowler=inn['bowl'][-1]['name'] if inn['bowl'] else '', inn=n)
    if prev and prev['inn'] == n:
        st.update(over=list(prev['over']), strike=prev['strike'], bowler=prev['bowler'])
        db, dr, dw = balls(st['ov']) - balls(prev['ov']), runs - prev['runs'], wk - prev['wk']
        for nm, o in st['bowl'].items():
            if prev['bowl'].get(nm) not in (None, o): st['bowler'] = nm
        if db >= 1:
            if balls(prev['ov']) % 6 == 0: st['over'] = []
            st['over'].append('W' if dw else str(dr) if db == 1 else '•')
            hit = [nm for nm, b in st['bat'].items() if nm in prev['bat'] and b['b'] > prev['bat'][nm]['b']]
            if hit:
                cur = hit[0]; br = st['bat'][cur]['r'] - prev['bat'][cur]['r']
                other = [x for x in st['bat'] if x != cur]
                st['strike'] = other[0] if other and ((br % 2) ^ (balls(st['ov']) % 6 == 0)) else cur
        elif dr > 0:
            st['over'].append('Wd' if st['ex']['w'] > prev['ex']['w'] else 'Nb' if st['ex']['nb'] > prev['ex']['nb'] else '+%d' % dr)
    elif not prev: st['over'] = []
    STATE[key] = st
    bw = next((b for b in inn['bowl'] if b['name'] == st['bowler']), dict(name='-', o='0', r=0, w=0))
    target = 0
    if n > 1:
        first = list(map(int, re.findall(r'\d+', p['teams'][0]['score'])))
        target = (first[0] if first else 0) + 1
    return dict(battingTeam=bt['name'], bowlingTeam=ot['name'], innings=n, runs=runs, wickets=wk, overs=bt['ov'], maxOvers=bt['max'], target=target,
                logo=p['logos'][n - 1] if len(p['logos']) >= n else '', status=p['status'],
                batters=[dict(name=b['name'], runs=b['r'], balls=b['b'], fours=b['f'], sixes=b['s'], onStrike=b['name'] == st['strike']) for b in at],
                bowler=dict(name=bw['name'], overs=bw['o'], runs=bw['r'], wickets=bw['w']), thisOver=st['over'])


ADMIN = '''<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="font:18px Arial;background:#061029;color:#fff;padding:20px">
<h2 style="color:#f2c96b">Set today's match</h2>Paste the CricClubs match link (any league, scorecard or live)<br>
<textarea id=l rows=4 style="font-size:16px;width:100%;margin:8px 0"></textarea><br>
Stream<br><select id=s style="font-size:20px;margin:8px 0"><option value=1>Stream 1 (overlay link ?auto=1)</option><option value=2>Stream 2 (overlay link ?auto=2)</option></select><br>
Admin key<br><input id=k type=password style="font-size:20px;width:100%;margin:8px 0"><br>
<button onclick="go()" style="font-size:22px;padding:12px 24px;background:#f2c96b;border:0;border-radius:10px">Set match</button><p id=o></p>
<script>k.value=localStorage.k||'';function go(){localStorage.k=k.value;fetch('/set?key='+encodeURIComponent(k.value)+'&slot='+s.value+'&link='+encodeURIComponent(l.value.trim())).then(r=>r.json()).then(j=>o.textContent=j.ok?'Done. Showing: '+j.link:'Error: '+j.error)}</script>'''
IDS = {}
def resolve(league, token, html=None):
    """New-style link (/League/results/<token>) -> (matchId, clubId) read from the page's own data."""
    if token in IDS: return IDS[token]
    if html is None:
        r = HTTP.get(f'https://cricclubs.com/{league}/results/{token}', timeout=15); r.raise_for_status(); html = r.text
    m = re.search(r'"clubID\\?":(\d+)[^{}]{0,250}?"matchID\\?":(\d+),\\?"encryptedMatchId\\?":\\?"' + re.escape(token), html)
    if not m: raise ValueError("couldn't find this match's numbers on that page")
    IDS[token] = (m.group(2), m.group(1)); return IDS[token]

CACHE, CUR = {}, os.path.join(HERE, 'current.json')
def current(slot='1'):
    try: return json.load(open(CUR)).get(slot, '')
    except Exception: return ''
def get_match(q):
    link = q.get('link', [''])[0] or (current(q.get('slot', ['1'])[0]) if not q.get('matchId') else '')
    if link:
        u = urlparse(link); league = u.path.strip('/').split('/')[0]; uq = parse_qs(u.query)
        mid, cid = uq.get('matchId', [''])[0], uq.get('clubId', [''])[0]
        seg = u.path.strip('/').split('/')
        if not mid and len(seg) >= 3 and seg[1] == 'results': mid, cid = resolve(league, seg[2])
    else:
        league, mid, cid = (q.get(k, [''])[0] for k in ('league', 'matchId', 'clubId'))
    key = (league, mid, cid)
    with LOCK:
        if key in CACHE and time.time() - CACHE[key][0] < 3: return CACHE[key][1]
        r = HTTP.get(f'https://cricclubs.com/{league}/viewScorecard.do?matchId={mid}&clubId={cid}', timeout=15)
        r.raise_for_status()
        out = live(key, parse(r.text)); CACHE[key] = (time.time(), out)
        return out


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == '/set':
                q = parse_qs(u.query); k = os.environ.get('ADMIN_KEY', '')
                if not k or q.get('key', [''])[0] != k: raise PermissionError('wrong or unset ADMIN_KEY')
                lk = q.get('link', [''])[0].strip(); uq = parse_qs(urlparse(lk).query)
                if 'cricclubs.com' not in lk or not ((uq.get('matchId') and uq.get('clubId')) or '/results/' in lk): raise ValueError('that does not look like a CricClubs match link (needs matchId and clubId)')
                try: d = json.load(open(CUR))
                except Exception: d = {}
                d[q.get('slot', ['1'])[0]] = lk; json.dump(d, open(CUR, 'w')); CACHE.clear(); STATE.clear(); body, ct = json.dumps(dict(ok=True, link=lk)).encode(), 'application/json'
            elif u.path == '/admin': body, ct = ADMIN.encode(), 'text/html; charset=utf-8'
            elif u.path == '/match': body, ct = json.dumps(get_match(parse_qs(u.query))).encode(), 'application/json'
            else: body, ct = open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8'
            self.send_response(200)
        except Exception as e:
            body, ct = json.dumps(dict(error=str(e))).encode(), 'application/json'; self.send_response(502)
        self.send_header('Content-Type', ct); self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


if __name__ == '__main__':
    if len(sys.argv) > 1:  # offline test:  python server.py saved_page.html
        print(json.dumps(live('t', parse(open(sys.argv[1], encoding='utf-8', errors='ignore').read())), indent=1)); sys.exit()
    port = int(os.environ.get('PORT', 8080)); print('Overlay server on port', port)
    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()
