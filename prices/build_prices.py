#!/usr/bin/env python3
"""Ashdod-area supermarket prices -> SQLite (Israel Price Transparency Law files: Stores, PriceFull, PromoFull).
Free, no keys. Raw files are streamed in memory and never kept on disk.
usage: build_prices.py OUT.db [--chains a,b] [--report report.json]"""
import requests, re, gzip, io, zipfile, json, sys, time, sqlite3, os, argparse, datetime as dt, traceback
from xml.etree import ElementTree as ET
requests.packages.urllib3.disable_warnings()
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'}
NOW = dt.datetime.utcnow() + dt.timedelta(hours=2)
# Ashdod + ~10 km
AREA_NAMES = ['אשדוד', 'גן יבנה', 'יבנה', 'ניצן', 'בני דרום', 'חצור אשדוד', 'חצור-אשדוד', 'באר גנים', 'שדה עוזיהו', 'גבעת וושינגטון',
              'כפר ורבורג', 'בית עזרא', 'ניר גלים', 'אמונים', 'עזריקם', 'שתולים', 'ASHDOD', 'Ashdod', 'YAVNE', 'Yavne']
AREA_CODES = {'70': 'אשדוד', '166': 'גן יבנה', '2660': 'יבנה', '1419': 'ניצן', '592': 'בני דרום', '406': 'חצור-אשדוד'}
MAX_STORES = int(os.environ.get('MAX_STORES', '12'))

def unpack(b):
    if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
    if b[:2] == b'PK':
        z = zipfile.ZipFile(io.BytesIO(b)); b = z.read(z.namelist()[0])
        if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
    return b
def parse_xml(b):
    b = unpack(b)
    if b[:2] in (b'\xff\xfe', b'\xfe\xff'): t = b.decode('utf-16')
    elif len(b) > 3 and b[1:2] == b'\x00': t = b.decode('utf-16-le')
    else:
        try: t = b.decode('utf-8')
        except UnicodeDecodeError: t = b.decode('cp1255', 'ignore')
    t = t.lstrip('\ufeff \r\n\t')
    t = re.sub(r'^<\?xml[^>]*\?>', '', t, count=1)
    root = ET.fromstring(t)
    for el in root.iter():
        el.tag = el.tag.split('}')[-1].lower()
    return root
def txt(el, tag):
    x = el.find(tag)
    return (x.text or '').strip() if x is not None and x.text else ''
def num(s):
    try: return float(str(s).replace(',', '').strip())
    except Exception: return 0.0
def clean(s): return re.sub(r'\s+', ' ', (s or '').replace('\u200f', '').replace('\u200e', '')).strip()

def store_rows(root):
    rows = []
    for st in list(root.iter('store')) + list(root.iter('branch')):
        sid = txt(st, 'storeid') or txt(st, 'storeId'.lower())
        if not sid: continue
        rows.append({'id': sid.lstrip('0') or '0', 'name': clean(txt(st, 'storename')), 'city': clean(txt(st, 'city')),
                     'address': clean(txt(st, 'address')), 'subchain': clean(txt(st, 'subchainname'))})
    return rows
def area_city(r):
    c = r['city']
    if c in AREA_CODES: return AREA_CODES[c]
    for n in AREA_NAMES:
        if n in c: return n
    for n in ('אשדוד',):
        if n in r['name'] or n in r['address']: return n
    return None
def pick_area(rows):
    out = []
    seen = set()
    for r in rows:
        city = area_city(r)
        if city and r['id'] not in seen:
            seen.add(r['id']); r = dict(r); r['city_name'] = city; out.append(r)
    out.sort(key=lambda r: (r['city_name'] != 'אשדוד', 'online' in r['name'].lower() or 'אונליין' in r['name'] or 'ONLINE' in r['name']))
    return out[:MAX_STORES]
def fdate(name):
    m = re.findall(r'(20\d{6})(\d{2,6})?', name or '')
    return ''.join(m[-1]) if m else ''

# ---------------- sources ----------------
class PP:  # url.publishedprices.co.il (Cerberus)
    BASE = 'https://url.publishedprices.co.il'
    def __init__(self, user, pw=''):
        self.s = requests.Session(); self.s.headers.update(UA); self.s.verify = False
        r = self.s.get(self.BASE + '/login', timeout=40)
        tok = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text).group(1)
        self.s.post(self.BASE + '/login/user', data={'username': user, 'password': pw, 'csrftoken': tok, 'Submit': 'Sign in'}, timeout=40)
        r = self.s.get(self.BASE + '/file', timeout=40)
        m = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text)
        if not m: raise RuntimeError('login failed for ' + user)
        self.tok = m.group(1)
        self._ls = {}
    def ls(self, search):
        if search not in self._ls:
            r = self.s.post(self.BASE + '/file/json/dir', data={'sEcho': 1, 'iColumns': 5, 'iDisplayStart': 0, 'iDisplayLength': 100000,
                            'sSearch': search, 'cd': '/', 'csrftoken': self.tok}, timeout=90)
            self._ls[search] = sorted(a['name'] for a in r.json().get('aaData', []))
        return self._ls[search]
    def get(self, name):
        r = self.s.get(self.BASE + '/file/d/' + name, timeout=180); r.raise_for_status(); return r.content
    def stores(self):
        names = [n for n in self.ls('Stores') if n.lower().startswith('stores')]
        names.sort(key=fdate)
        if not names: raise RuntimeError('no Stores file')
        return store_rows(parse_xml(self.get(names[-1])))
    def latest(self, kind, store_id):
        pat = re.compile(r'^%s\d+-\d+-0*%s-' % (kind, store_id), re.I)
        names = [n for n in self.ls(kind) if pat.match(n)]
        names.sort(key=fdate)
        return (names[-1], self.get(names[-1])) if names else (None, None)

class Shufersal:
    BASE = 'https://prices.shufersal.co.il'
    def __init__(self): self.s = requests.Session(); self.s.headers.update(UA)
    def links(self, cat, store=0):
        out = []
        for page in range(1, 4):
            r = self.s.get(self.BASE + '/FileObject/UpdateCategory', params={'catID': cat, 'storeId': store, 'page': page}, timeout=60)
            l = [x.replace('&amp;', '&') for x in re.findall(r'href="(https://pricesprodpublic[^"]+)"', r.text)]
            out += l
            if len(l) < 20: break
        return out
    def stores(self):
        l = self.links(5)
        return store_rows(parse_xml(self.s.get(l[0], timeout=90).content))
    def latest(self, kind, store_id):
        cat = 2 if kind == 'PriceFull' else 4
        l = [x for x in self.links(cat, store_id) if kind.lower() in x.lower()]
        if not l: return None, None
        l.sort(key=lambda u: fdate(u.split('?')[0]))
        u = l[-1]; return u.split('/')[-1].split('?')[0], self.s.get(u, timeout=180).content

class Bina:  # *.binaprojects.com (King Store, Maayan 2000, Zol Vebegadol, Shuk Hayir, ...)
    def __init__(self, host):
        self.base = 'https://%s.binaprojects.com' % host
        self.s = requests.Session(); self.s.headers.update(UA); self.s.verify = False
        self._files = None
    def files(self):
        if self._files is None:
            out = []
            for d in range(0, 3):
                day = (NOW - dt.timedelta(days=d)).strftime('%d/%m/%Y')
                r = self.s.post(self.base + '/MainIO_Hok.aspx', data={'WStore': '', 'WDate': day, 'WFileType': '0'}, timeout=60)
                try: js = r.json()
                except Exception: js = []
                out += [x.get('FileNm') for x in js if x.get('FileNm')]
                if out: break
            self._files = sorted(set(out), key=fdate)
        return self._files
    def get(self, name):
        r = self.s.get(self.base + '/Download.aspx', params={'FileNm': name}, timeout=60)
        try:
            js = r.json(); url = js[0]['SPath']
            return self.s.get(url, timeout=180).content
        except Exception:
            return r.content
    def stores(self):
        names = [n for n in self.files() if n.lower().startswith('store')]
        if not names: raise RuntimeError('no Stores file today')
        return store_rows(parse_xml(self.get(names[-1])))
    def latest(self, kind, store_id):
        pat = re.compile(r'^%s\d+-\d+-0*%s-' % (kind, store_id), re.I)
        names = [n for n in self.files() if pat.match(n)]
        return (names[-1], self.get(names[-1])) if names else (None, None)

class Listing:  # generic portal: an HTML page listing direct .gz/.xml links (Carrefour, Laib/Victory, Hatzi Hinam)
    def __init__(self, pages, link_re=r'href="([^"]+?\.(?:gz|xml|zip)[^"]*)"'):
        self.pages = pages; self.link_re = link_re
        self.s = requests.Session(); self.s.headers.update(UA); self.s.verify = False
        self._links = None
    def links(self):
        if self._links is None:
            out = []
            for p in self.pages:
                r = self.s.get(p, timeout=60)
                for l in re.findall(self.link_re, r.text, re.I):
                    l = l.replace('&amp;', '&').replace('\\', '/')
                    out.append(requests.compat.urljoin(p, l))
            self._links = out
        return self._links
    def _named(self, kind):
        return [l for l in self.links() if re.search(r'/%s[^/]*$' % kind, l.split('?')[0], re.I)]
    def get(self, url): return self.s.get(url, timeout=180).content
    def stores(self):
        l = sorted(self._named('stores?'), key=lambda u: fdate(u))
        if not l: raise RuntimeError('no Stores link on page (%d links)' % len(self.links()))
        return store_rows(parse_xml(self.get(l[-1])))
    def latest(self, kind, store_id):
        pat = re.compile(r'/%s\d+-\d+-0*%s-[^/]*$' % (kind, store_id), re.I)
        l = sorted([u for u in self.links() if pat.search(u.split('?')[0])], key=lambda u: fdate(u.split('?')[0]))
        return (l[-1].split('/')[-1].split('?')[0], self.get(l[-1])) if l else (None, None)

CHAINS = [  # id, Hebrew name, factory
    ('shufersal', 'שופרסל', lambda: Shufersal()),
    ('ramilevy', 'רמי לוי', lambda: PP('RamiLevi')),
    ('yohananof', 'יוחננוף', lambda: PP('yohananof')),
    ('osherad', 'אושר עד', lambda: PP('osherad')),
    ('tivtaam', 'טיב טעם', lambda: PP('TivTaam')),
    ('freshmarket', 'פרש מרקט', lambda: PP('freshmarket')),
    ('doralon', 'דור אלון (AM:PM / אלונית)', lambda: PP('doralon')),
    ('keshet', 'קשת טעמים', lambda: PP('Keshet')),
    ('salachd', 'סאלח דבאח', lambda: PP('SalachD', '12345')),
    ('stopmarket', 'סטופ מרקט', lambda: PP('Stop_Market')),
    ('politzer', 'פוליצר', lambda: PP('politzer')),
    ('yellow', 'Yellow (פז)', lambda: PP('Paz_bo', 'paz468')),
    ('cofix', 'סופר קופיקס', lambda: PP('SuperCofixApp')),
    ('yuda', 'סופר יודה', lambda: PP('yuda_ho', 'Yud@147')),
    ('kingstore', 'קינג סטור', lambda: Bina('kingstore')),
    ('maayan2000', 'מעיין 2000', lambda: Bina('maayan2000')),
    ('zolvebegadol', 'זול ובגדול', lambda: Bina('zolvebegadol')),
    ('shukhayir', 'שוק העיר', lambda: Bina('shuk-hayir')),
    ('superbareket', 'סופר ברקת', lambda: Bina('superbareket')),
    ('victory', 'ויקטורי', lambda: Listing(['https://laibcatalog.co.il/'])),
    ('carrefour', 'קרפור (כולל יינות ביתן / מגה)', lambda: Listing(['https://prices.carrefour.co.il/'])),
    ('hatzihinam', 'חצי חינם', lambda: Listing(['https://shop.hazi-hinam.co.il/Prices'])),
]

# ---------------- normalisation ----------------
def units(it):
    """-> (unit_kind, size_in_base, base_label) where base is kg / liter / unit; size None if unknown."""
    name = txt(it, 'itemname'); uom = txt(it, 'unitofmeasure'); qty = num(txt(it, 'quantity'))
    uqty = txt(it, 'unitqty'); w = txt(it, 'bisweighted') == '1'
    blob = (uom + ' ' + uqty).replace('"', '').replace("'", '').replace('׳', '').replace('״', '')
    if w: return 'kg', 1.0, True
    if re.search(r'100\s*(גר|גרם|gr|g)\b', blob, re.I) and qty > 0: return 'kg', qty / 1000.0, False
    if re.search(r'100\s*(מל|מ\"ל|ml)', blob, re.I) and qty > 0: return 'l', qty / 1000.0, False
    if re.search(r'קג|קילו|kg', blob, re.I) and qty > 0: return 'kg', qty, False
    if re.search(r'גר|gr|גרם', blob, re.I) and qty > 0: return 'kg', qty / 1000.0, False
    if re.search(r'מל|ml|מיליליטר', blob, re.I) and qty > 0: return 'l', qty / 1000.0, False
    if re.search(r'ליטר|ltr|\bl\b|לטר', blob, re.I) and qty > 0: return 'l', qty, False
    n = name.replace('"', '').replace("'", '')
    m = re.search(r'(\d+(?:\.\d+)?)\s*(קג|קילו|ק\b)', n)
    if m: return 'kg', float(m.group(1)), False
    m = re.search(r'(\d+(?:\.\d+)?)\s*(גרם|גר|ג\b|gr|g\b)', n, re.I)
    if m and float(m.group(1)) > 0: return 'kg', float(m.group(1)) / 1000, False
    m = re.search(r'(\d+(?:\.\d+)?)\s*(מל|מ\b|ml)', n, re.I)
    if m and float(m.group(1)) > 0: return 'l', float(m.group(1)) / 1000, False
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ליטר|ל\b|lt?r?\b)', n, re.I)
    if m and float(m.group(1)) > 0: return 'l', float(m.group(1)), False
    if re.search(r'יח|unit', blob, re.I) and qty > 0: return 'unit', qty, False
    return 'unit', None, False

def parse_promos(root):
    """-> {itemcode: [promo dict]} for promos active today; blanket (>300 item) offers skipped."""
    out = {}
    for p in root.iter('promotion'):
        elems = list(p.iter('item')) + list(p.iter('promotionitem'))
        if not elems or len(elems) > 300: continue
        def pd(tag):
            s = (txt(p, tag) or '').strip()[:10]
            for f in ('%Y-%m-%d', '%d/%m/%Y', '%Y/%m/%d'):
                try: return dt.datetime.strptime(s, f)
                except Exception: pass
            return None
        end = pd('promotionenddate') or pd('promotionenddatetime'); start = pd('promotionstartdate') or pd('promotionstartdatetime')
        if end and end.date() < NOW.date(): continue
        if start and start.date() > NOW.date(): continue
        club = txt(p, 'clubid'); clubs = [txt(c, 'clubid') for c in p.iter('clubs')]
        allc = [c for c in [club] + clubs if c]
        is_club = bool(allc) and not any(c in ('0', '00', '000') or c.startswith('0 ') or 'כלל' in c for c in allc)
        coupon = txt(p, 'additionaliscoupon') == '1'
        desc = clean(txt(p, 'promotiondescription'))[:90]
        g = lambda tag: num(txt(p, tag))
        base = {'desc': desc, 'end': end.strftime('%Y-%m-%d') if end else '', 'club': 1 if is_club else 0, 'coupon': 1 if coupon else 0,
                'min_qty': g('minqty'), 'max_qty': g('maxqty'), 'disc_price': g('discountedprice'),
                'disc_rate': g('discountrate'), 'reward': txt(p, 'rewardtype'), 'id': txt(p, 'promotionid')}
        for e in elems:
            code = txt(e, 'itemcode')
            if not code: continue
            d = dict(base)
            for t in ('minqty', 'discountedprice', 'discountrate'):
                v = txt(e, t)
                if v: d[{'minqty': 'min_qty', 'discountedprice': 'disc_price', 'discountrate': 'disc_rate'}[t]] = num(v)
            if txt(e, 'isgiftitem') == '1': continue
            out.setdefault(code, []).append(d)
    return out

def promo_unit_price(price, pr):
    """price per item under the promo (and the qty needed), or (None, None) when not computable honestly."""
    q = pr['min_qty'] if pr['min_qty'] and pr['min_qty'] >= 1 else 1
    dp, rate, rw = pr['disc_price'], pr['disc_rate'], pr['reward']
    if dp > 0:
        if q > 1 and dp > price * 1.02:  # bundle total, e.g. "2 ב-30"
            u = dp / q
        else:
            u = dp
        if 0.2 * price <= u < price * 0.995: return round(u, 2), q
    if rw in ('2', '3') and rate > 0:
        r = rate / 100.0 if rate > 1 else rate
        if r > 1: r = r / 100.0
        if 0 < r < 0.8: return round(price * (1 - r), 2), q
    return None, None

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores(chain_id TEXT, chain TEXT, store_id TEXT, name TEXT, city TEXT, address TEXT, price_file TEXT, promo_file TEXT, price_date TEXT, n_items INT, PRIMARY KEY(chain_id, store_id));
CREATE TABLE IF NOT EXISTS items(chain_id TEXT, store_id TEXT, code TEXT, name TEXT, brand TEXT, price REAL, unit_kind TEXT, size REAL, weighted INT, updated TEXT);
CREATE TABLE IF NOT EXISTS promos(chain_id TEXT, store_id TEXT, code TEXT, descr TEXT, end_date TEXT, club INT, coupon INT, min_qty REAL, unit_price REAL);
CREATE TABLE IF NOT EXISTS chains(chain_id TEXT PRIMARY KEY, chain TEXT, status TEXT, note TEXT, n_stores INT, n_items INT, seconds REAL, built_at TEXT, built_where TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

def run_chain(db, cid, cname, src, where):
    t0 = time.time(); n_items = 0; stores_ok = 0; notes = []
    rows = src.stores()
    picked = pick_area(rows)
    if not picked:
        return 'no_local_branch', 'אין סניף באזור אשדוד בקובץ הסניפים (%d סניפים בקובץ)' % len(rows), 0, 0
    for st in picked:
        try:
            pname, pb = src.latest('PriceFull', st['id'])
            if not pb: notes.append('%s: no PriceFull' % st['id']); continue
            root = parse_xml(pb); del pb
            batch = []
            codes = set()
            for it in root.iter('item'):
                code = txt(it, 'itemcode'); price = num(txt(it, 'itemprice'))
                if not code or price <= 0: continue
                name = clean(txt(it, 'itemname') or txt(it, 'itemnm'))
                if not name: continue
                uk, size, w = units(it)
                batch.append((cid, st['id'], code, name[:120], clean(txt(it, 'manufacturername'))[:40], price, uk, size, 1 if w else 0,
                              txt(it, 'priceupdatedate')[:16]))
                codes.add(code)
            del root
            if not batch: notes.append('%s: empty' % st['id']); continue
            db.execute('DELETE FROM items WHERE chain_id=? AND store_id=?', (cid, st['id']))
            db.execute('DELETE FROM promos WHERE chain_id=? AND store_id=?', (cid, st['id']))
            db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)', batch)
            prices = {b[2]: b[5] for b in batch}
            mname = None
            try:
                mname, mb = src.latest('PromoFull', st['id'])
                if mb:
                    pm = parse_promos(parse_xml(mb)); del mb
                    prow = []
                    for code, lst in pm.items():
                        if code not in prices: continue
                        for pr in lst:
                            u, q = promo_unit_price(prices[code], pr)
                            prow.append((cid, st['id'], code, pr['desc'], pr['end'], pr['club'], pr['coupon'], q or pr['min_qty'] or 1, u))
                    db.executemany('INSERT INTO promos VALUES (?,?,?,?,?,?,?,?,?)', prow)
            except Exception as e:
                notes.append('%s promo: %s' % (st['id'], str(e)[:80])); mname = None
            pdate = fdate(pname)[:8]
            db.execute('INSERT OR REPLACE INTO stores VALUES (?,?,?,?,?,?,?,?,?,?)', (cid, cname, st['id'], st['name'], st['city_name'], st['address'],
                       pname, mname, pdate, len(batch)))
            db.commit(); n_items += len(batch); stores_ok += 1
        except Exception as e:
            notes.append('%s: %s' % (st['id'], (type(e).__name__ + ' ' + str(e))[:100]))
    return ('ok' if stores_ok else 'no_file'), '; '.join(notes)[:400], stores_ok, n_items

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('out'); ap.add_argument('--chains', default=''); ap.add_argument('--where', default='local')
    ap.add_argument('--only-failed', action='store_true', help='only chains not ok in OUT already')
    a = ap.parse_args()
    db = sqlite3.connect(a.out); db.executescript(SCHEMA)
    want = set(filter(None, a.chains.split(',')))
    if a.only_failed:
        ok = {r[0] for r in db.execute("SELECT chain_id FROM chains WHERE status='ok'")}
        want = {c for c, _, _ in CHAINS if c not in ok} if not want else want - ok
    for cid, cname, mk in CHAINS:
        if want and cid not in want: continue
        t0 = time.time()
        try:
            st, note, ns, ni = run_chain(db, cid, cname, mk(), a.where)
        except Exception as e:
            st, note, ns, ni = 'error', (type(e).__name__ + ': ' + str(e))[:200], 0, 0
        prev = db.execute('SELECT status FROM chains WHERE chain_id=?', (cid,)).fetchone()
        if st != 'ok' and prev and prev[0] == 'ok':
            print(cid, 'kept previous ok data;', st, note, file=sys.stderr); continue
        db.execute('INSERT OR REPLACE INTO chains VALUES (?,?,?,?,?,?,?,?,?)', (cid, cname, st, note, ns, ni, round(time.time() - t0, 1),
                   dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), a.where))
        db.commit()
        print(cid, st, ns, ni, round(time.time() - t0, 1), note[:200], file=sys.stderr, flush=True)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),))
    db.execute('CREATE INDEX IF NOT EXISTS ix_items_code ON items(code)')
    db.execute('CREATE INDEX IF NOT EXISTS ix_promos ON promos(chain_id, store_id, code)')
    db.commit(); db.execute('VACUUM'); db.close()

if __name__ == '__main__': main()
