#!/usr/bin/env python3
"""Builds meat.json: chicken & beef prices + promos from Israel's Price Transparency Law files.
Free, no keys: Shufersal public site + url.publishedprices.co.il (public chain users, empty password)."""
import requests, re, gzip, io, zipfile, json, sys, time, datetime as dt
from xml.etree import ElementTree as ET
UA = {'User-Agent': 'Mozilla/5.0 (hoskim-price-compare; personal use)'}
TODAY = dt.datetime.utcnow() + dt.timedelta(hours=2)
CITY = 'אשדוד'; CITY_CODES = {'70'}
PP_CHAINS = [  # (id, Hebrew name, publishedprices user)
    ('ramilevy', 'רמי לוי', 'RamiLevi'), ('osherad', 'אושר עד', 'osherad'),
    ('yohananof', 'יוחננוף', 'yohananof'), ('tivtaam', 'טיב טעם', 'TivTaam'),
    ('freshmarket', 'פרש מרקט', 'freshmarket'), ('doralon', 'אלונית/דור אלון', 'doralon'),
    ('keshet', 'קשת טעמים', 'Keshet'), ('stopmarket', 'סטופ מרקט', 'Stop_Market'),
]
# ---- product classification -------------------------------------------------
CATS = [  # key, label, include regex, exclude regex
 ('breast', 'חזה עוף', r'חזה\s*עוף|פילה\s*עוף|נתחי\s*חזה\s*עוף', r'הודו|טחון'),
 ('schnitzel', 'שניצל', r'שניצל', r'צומח|ציפוי|לחמני|חלות|פירור|פרור|לחם|צמחי|טבעול|תירס|סויה|ירקות|כרובית|דג|סלמון|ברוקולי|חציל|vegan|טבעוני|תפו"?א|גבינ'),
 ('thighs', 'פרגיות / ירכיים', r'פרגי|\bירכי|\bירך\s*עוף|\bשוקיים', r'הודו|כנף'),
 ('whole', 'עוף שלם', r'עוף\s*שלם|עוף\s*טרי\s*שלם|עוף\s*שלם', r''),
 ('ground', 'בשר טחון', r'(בקר|עגל|בשר)\s*\S*\s*טחון|טחון\s*(בקר|עגל|בשר)', r'אמנון|עוף|הודו|דג|צמחי|טבעול|סויה|פלפל|אגוז|שקד|קפה|תבלין|מלח|שומשום|חומוס|כמון|פרג|חלבה|קינמון|ג.ינג.ר|מוצרלה|גבינ'),
 ('steak', 'סטייק / אנטריקוט', r'אנטריקוט|סטייק|סינטה|פילה\s*בקר|שייטל|ריב\s*איי', r'צ.?יפס|תערובת|תיבול|בורגר|עוף|הודו|דג|סלמון|טונה|צמחי|טבעול|רוטב|תבלין|כרובית|חציל|פטריות|סויה|פרגית'),
]
GLOBAL_EX = r'נקניק|פסטרמה|מעושן|צלוי|מבושל|מרק|אבקת|תבלין|רוטב|קבב|המבורגר|פשטידה|כריך|סלט|מאפה|בורקס|מיני\s*שניצל\s*ל?כלב|לכלב|לחתול|חטיף|מזון\s*ל|פיצה|פסטה|ממרח|טורטיה|כופתאות|קציצ|נאגטס|נגטס|פרוס|שווארמה|ג.רקי|ביף|קבנוס|מוקפץ|מטוגן|ברוטב|בתיבול|ממולא'
def classify(name):
    n = name or ''
    if re.search(GLOBAL_EX, n): return None
    for key, label, inc, exc in CATS:
        if re.search(inc, n) and not (exc and re.search(exc, n)):
            if key == 'breast' and 'שניצל' in n: return 'schnitzel'
            return key
    return None
# ---- helpers ----------------------------------------------------------------
def unpack(b):
    if b[:2] == b'\x1f\x8b': b = gzip.decompress(b)
    if b[:2] == b'PK':
        z = zipfile.ZipFile(io.BytesIO(b)); b = z.read(z.namelist()[0])
    return b
def parse_xml(b):
    b = unpack(b)
    if b[:2] in (b'\xff\xfe', b'\xfe\xff'): t = b.decode('utf-16')
    elif len(b) > 3 and b[1:2] == b'\x00': t = b.decode('utf-16-le')
    else: t = b.decode('utf-8', 'ignore')
    t = re.sub(r'^\ufeff', '', t.lstrip())
    t = re.sub(r'<\?xml[^>]*\?>', '', t, count=1)
    root = ET.fromstring(t)
    for el in root.iter():  # lowercase + drop namespaces
        el.tag = el.tag.split('}')[-1].lower()
    return root
def txt(el, tag):
    x = el.find(tag)
    return (x.text or '').strip() if x is not None and x.text else ''
def num(s):
    try: return float(str(s).replace(',', ''))
    except Exception: return 0.0
def per_kg(item):
    """price per kg for an item element, or None."""
    price = num(txt(item, 'itemprice')); uom = txt(item, 'unitofmeasure'); qty = num(txt(item, 'quantity'))
    uqty = txt(item, 'unitqty'); upp = num(txt(item, 'unitofmeasureprice')); w = txt(item, 'bisweighted')
    name = txt(item, 'itemname')
    if price <= 0: return None, price, False
    weighted = w == '1'
    blob = (uom + ' ' + uqty).replace('"', '').replace("'", '')
    if weighted and qty in (0, 1): return price, price, True  # weighted items are priced per kg
    if upp > 0 and re.search(r'100\s*גר', blob): return round(upp * 10, 2), price, weighted
    if upp > 0 and re.search(r'קג|קילו|kg', blob, re.I): return round(upp, 2), price, weighted
    if qty > 0:
        if re.search(r'גר|gr', blob, re.I): return round(price / qty * 1000, 2), price, weighted
        if re.search(r'קג|קילו|kg', blob, re.I): return round(price / qty, 2), price, weighted
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ק"?ג|קג|קילו|גר|גרם)', name)
    if m:
        g = float(m.group(1)) * (1000 if m.group(2).startswith('ק') else 1)
        if g > 0: return round(price / g * 1000, 2), price, weighted
    return None, price, weighted
def store_rows(root):
    rows = []
    for st in root.iter('store'):
        rows.append({'id': txt(st, 'storeid').lstrip('0') or '0', 'name': txt(st, 'storename'),
                     'city': txt(st, 'city'), 'address': txt(st, 'address')})
    return rows
def pick_stores(rows, n=2):
    ash = [r for r in rows if CITY in r['city'] or r['city'] in CITY_CODES or CITY in r['name'] or CITY in r['address']]
    return ash[:n], bool(ash)
# ---- sources ----------------------------------------------------------------
class PP:
    BASE = 'https://url.publishedprices.co.il'
    def __init__(self, user):
        self.user = user; self.s = requests.Session(); self.s.headers.update(UA)
        r = self.s.get(self.BASE + '/login', timeout=30)
        tok = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text).group(1)
        self.s.post(self.BASE + '/login/user', data={'username': user, 'password': '', 'csrftoken': tok, 'Submit': 'Sign in'}, timeout=30)
        r = self.s.get(self.BASE + '/file', timeout=30)
        self.tok = re.search(r'name="csrftoken"\s+content="([^"]+)"', r.text).group(1)
    def ls(self, search):
        r = self.s.post(self.BASE + '/file/json/dir', data={'sEcho': 1, 'iColumns': 5, 'iDisplayStart': 0, 'iDisplayLength': 100000,
                        'sSearch': search, 'cd': '/', 'csrftoken': self.tok}, timeout=60)
        return sorted(a['name'] for a in r.json().get('aaData', []))
    def get(self, name):
        r = self.s.get(self.BASE + '/file/d/' + name, timeout=120); r.raise_for_status(); return r.content
    def stores(self):
        names = [n for n in self.ls('Stores') if n.lower().startswith('stores')]
        return store_rows(parse_xml(self.get(names[-1])))
    def latest(self, kind, store_id):
        pat = re.compile(r'^%s\d+-\d+-0*%s-' % (kind, store_id))
        names = [n for n in self.ls(kind) if pat.match(n)]
        names.sort(key=lambda n: n.split('-')[-1])
        return (names[-1], self.get(names[-1])) if names else (None, None)
class Shufersal:
    BASE = 'https://prices.shufersal.co.il'
    def __init__(self): self.s = requests.Session(); self.s.headers.update(UA)
    def links(self, cat, store=0):
        r = self.s.get(self.BASE + '/FileObject/UpdateCategory', params={'catID': cat, 'storeId': store}, timeout=60)
        return [l.replace('&amp;', '&') for l in re.findall(r'href="(https://pricesprodpublic[^"]+)"', r.text)]
    def stores(self):
        l = self.links(5)
        return store_rows(parse_xml(self.s.get(l[0], timeout=60).content))
    def latest(self, kind, store_id):
        cat = 2 if kind == 'PriceFull' else 4
        l = self.links(cat, store_id)
        l = [x for x in l if kind.lower() in x.lower()]
        if not l: return None, None
        l.sort(key=lambda u: re.search(r'-(\d{8,})', u).group(1) if re.search(r'-(\d{8,})', u) else '')
        u = l[-1]; return u.split('/')[-1].split('?')[0], self.s.get(u, timeout=120).content
# ---- promos -----------------------------------------------------------------
def parse_date(s):
    s = (s or '').strip()[:10]
    for f in ('%Y-%m-%d', '%d/%m/%Y'):
        try: return dt.datetime.strptime(s, f)
        except Exception: pass
    return None
def promos_for(root, codes):
    out = {}
    for p in root.iter('promotion'):
        elems = list(p.iter('item')) + list(p.iter('promotionitem'))
        hit = [e for e in elems if txt(e, 'itemcode') in codes]
        if not hit or len(elems) > 150: continue  # store-wide blanket offers (vouchers, brand %) are not meat deals
        end = parse_date(txt(p, 'promotionenddate') or txt(p, 'promotionenddatetime'))
        start = parse_date(txt(p, 'promotionstartdate') or txt(p, 'promotionstartdatetime'))
        if end and end.date() < TODAY.date(): continue
        if start and start.date() > TODAY.date(): continue
        club = txt(p, 'clubid')
        club_txt = '' if club in ('', '0', '00', '0 - כלל הלקוחות') or club.startswith('0 ') else 'מועדון'
        coupon = txt(p, 'additionaliscoupon') == '1'
        def f(e, tag):
            v = txt(e, tag) or txt(p, tag)
            return num(v)
        for e in hit:
            d = {'desc': re.sub(r'\s+', ' ', txt(p, 'promotiondescription'))[:80], 'end': end.strftime('%d.%m') if end else '',
                 'min_qty': f(e, 'minqty'), 'price': f(e, 'discountedprice'),
                 'price_per_mido': f(e, 'discountedpricepermido') or f(e, 'discountedpricepermida'),
                 'club': club_txt or ('קופון' if coupon else '')}
            out.setdefault(txt(e, 'itemcode'), []).append(d)
    return out
def promo_kg(item_perkg, item_price, pr):
    """best-effort promo price per kg; None when it can't be computed honestly."""
    if not item_perkg or not item_price or pr['price'] <= 0: return None
    q = pr['min_qty'] if pr['min_qty'] >= 1 else 1
    unit = pr['price'] / q
    if unit >= item_price * 0.99: return None
    v = unit * (item_perkg / item_price)
    return round(v, 2) if 0.3 * item_perkg < v < item_perkg else None
# ---- main -------------------------------------------------------------------
def run_chain(cid, cname, src):
    info = {'id': cid, 'name': cname, 'status': 'ok', 'stores': [], 'items': []}
    rows = src.stores()
    picked, is_city = pick_stores(rows)
    if not picked:
        info['status'] = 'no_local_branch'; info['note'] = 'אין סניף באשדוד בקובץ הסניפים של הרשת'; return info
    for st in picked:
        pname, pb = src.latest('PriceFull', st['id'])
        if not pb: continue
        root = parse_xml(pb)
        items = []
        for it in root.iter('item'):
            name = txt(it, 'itemname'); cat = classify(name)
            if not cat: continue
            kg, price, weighted = per_kg(it)
            if kg is None or not (8 <= kg <= 450): continue
            items.append({'cat': cat, 'code': txt(it, 'itemcode'), 'name': re.sub(r'\s+', ' ', name)[:60],
                          'brand': txt(it, 'manufacturername')[:30], 'price': round(price, 2), 'per_kg': kg,
                          'weighted': weighted, 'updated': txt(it, 'priceupdatedate')[:10]})
        promo_name, mb = src.latest('PromoFull', st['id'])
        promo_ok = False
        if mb:
            try:
                pm = promos_for(parse_xml(mb), {i['code'] for i in items}); promo_ok = True
                for i in items:
                    ps = pm.get(i['code'], [])
                    best = None
                    for pr in ps:
                        k = promo_kg(i['per_kg'], i['price'], pr)
                        cand = {'desc': pr['desc'], 'end': pr['end'], 'club': pr['club'], 'per_kg': k}
                        if best is None or (k and (best['per_kg'] is None or k < best['per_kg'])): best = cand
                    if best: i['promo'] = best
            except Exception as e:
                info['promo_error'] = str(e)[:120]
        st2 = dict(st); st2['price_file'] = pname; st2['promo_file'] = promo_name if promo_ok else None
        info['stores'].append(st2)
        for i in items: i['store'] = st['name']
        info['items'].extend(items)
        break  # one branch per chain keeps the file small and comparable
    if not info['stores']: info['status'] = 'no_file'
    return info
def main(out='meat.json'):
    chains = []
    srcs = [('shufersal', 'שופרסל', Shufersal)] + [(c, n, (lambda u=u: PP(u))) for c, n, u in PP_CHAINS]
    for cid, cname, mk in srcs:
        t0 = time.time()
        try: info = run_chain(cid, cname, mk())
        except Exception as e:
            info = {'id': cid, 'name': cname, 'status': 'error', 'error': (type(e).__name__ + ': ' + str(e))[:160], 'stores': [], 'items': []}
        info['seconds'] = round(time.time() - t0, 1)
        print(cid, info['status'], len(info['items']), info.get('error', ''), info['seconds'], file=sys.stderr)
        chains.append(info)
    for cid, cname in [('victory', 'ויקטורי'), ('hatzihinam', 'חצי חינם')]:
        chains.append({'id': cid, 'name': cname, 'status': 'unreachable', 'note': 'אתר המחירים של הרשת חוסם קריאה אוטומטית', 'stores': [], 'items': []})
    # compact: per category per chain keep 10 cheapest (by promo or regular per kg)
    cats = {}
    for c in chains:
        for i in c['items']:
            eff = i.get('promo', {}).get('per_kg') or i['per_kg']; i['eff'] = eff
        by = {}
        for i in c['items']: by.setdefault(i['cat'], []).append(i)
        c['items'] = []
        for k, lst in by.items():
            lst.sort(key=lambda i: i['eff'])
            c['items'].extend(lst[:10])
    data = {'generated_at': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'city': CITY,
            'categories': [{'key': k, 'label': l} for k, l, _, _ in CATS],
            'source': 'חוק המזון (שקיפות מחירים) — קובצי PriceFull/PromoFull שהרשתות מפרסמות',
            'chains': chains}
    with open(out, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    print('wrote', out, sum(len(c['items']) for c in chains), 'items', file=sys.stderr)
if __name__ == '__main__': main(*(sys.argv[1:2]))
