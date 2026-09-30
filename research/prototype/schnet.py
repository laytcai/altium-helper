"""Prototype: build a netlist from an Altium .PrjPcb + .SchDoc files (no Altium needed).

Handles: wires, junction/T-connections, net labels (sheet-local), power ports (global),
ports <-> sheet entries (hierarchical), bus ports/entries expanded by member name.
Not handled (yet): harnesses, off-sheet connectors, multi-channel sheets, hidden power pins,
flat/global net-scope modes, directives.
"""
import collections, configparser, os, re, sys
from schdoc_dump import read_records


def num(r, k):
    # exact integer math in 1e-5 units of Altium's 10-mil grid (floats break equality tests)
    return int(r.get(k, 0) or 0) * 100000 + int(r.get(k + '_FRAC', 0) or 0)


class DSU:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def on_segment(pt, a, b):
    (x, y), (x1, y1), (x2, y2) = pt, a, b
    if (x2 - x1) * (y - y1) != (y2 - y1) * (x - x1):
        return False
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


def expand_bus(name):
    m = re.fullmatch(r'(.*?)\[(\d+)\.\.(\d+)\]', name or '')
    if not m: return None
    base, a, b = m.group(1), int(m.group(2)), int(m.group(3))
    step = 1 if b >= a else -1
    return [f'{base}{i}' for i in range(a, b + step, step)]


class Sheet:
    def __init__(self, path, sid):
        self.path, self.sid = path, sid
        _, recs = read_records(path)
        self.recs = recs[1:]
        self.pins, self.labels, self.power, self.ports = [], [], [], []
        self.entries, self.symbols = [], []
        self.wires, self.buses, self.junctions = [], [], []
        self._parse()

    def _parse(self):
        R = self.recs
        kids = collections.defaultdict(list)
        for i, r in enumerate(R):
            if 'OWNERINDEX' in r: kids[r['OWNERINDEX']].append((i, r))
        for i, r in enumerate(R):
            t = r.get('RECORD')
            if t == '1':  # component
                part, mode = r.get('CURRENTPARTID', '1'), r.get('DISPLAYMODE', '0')
                des = next((k.get('TEXT') for _, k in kids[str(i)] if k.get('RECORD') == '34'), '?')
                for _, p in kids[str(i)]:
                    if p.get('RECORD') != '2': continue
                    if p.get('OWNERPARTID', '1') not in (part, '0', '-1'): continue
                    if p.get('OWNERPARTDISPLAYMODE', '0') != mode: continue
                    x, y, L = num(p, 'LOCATION.X'), num(p, 'LOCATION.Y'), num(p, 'PINLENGTH')
                    o = int(p.get('PINCONGLOMERATE', '0')) & 3
                    dx, dy = [(1, 0), (0, 1), (-1, 0), (0, -1)][o]
                    self.pins.append(dict(des=des, num=p.get('DESIGNATOR', ''), name=p.get('NAME', ''),
                                          pt=(x + dx * L, y + dy * L), comp=r.get('LIBREFERENCE', '')))
            elif t == '27' or t == '26':
                n = int(r.get('LOCATIONCOUNT', '0'))
                pts = [(num(r, f'X{k}'), num(r, f'Y{k}')) for k in range(1, n + 1)]
                (self.wires if t == '27' else self.buses).append(pts)
            elif t == '29':
                self.junctions.append((num(r, 'LOCATION.X'), num(r, 'LOCATION.Y')))
            elif t == '25':
                self.labels.append((r.get('TEXT', ''), (num(r, 'LOCATION.X'), num(r, 'LOCATION.Y'))))
            elif t == '17':
                self.power.append((r.get('TEXT', ''), (num(r, 'LOCATION.X'), num(r, 'LOCATION.Y'))))
            elif t == '18':
                x, y, w = num(r, 'LOCATION.X'), num(r, 'LOCATION.Y'), num(r, 'WIDTH')
                vertical = int(r.get('STYLE', '0')) >= 4
                ends = [(x, y), (x, y - w), (x, y + w)] if vertical else [(x, y), (x + w, y)]
                self.ports.append((r.get('NAME', ''), ends))
            elif t == '15':
                x, y = num(r, 'LOCATION.X'), num(r, 'LOCATION.Y')
                xs, ys = num(r, 'XSIZE'), num(r, 'YSIZE')
                fname = next((k.get('TEXT') for _, k in kids[str(i)] if k.get('RECORD') == '33'), None)
                sname = next((k.get('TEXT') for _, k in kids[str(i)] if k.get('RECORD') == '32'), None)
                sym = dict(file=fname, name=sname, entries=[])
                for _, e in kids[str(i)]:
                    if e.get('RECORD') != '16': continue
                    d = num(e, 'DISTANCEFROMTOP') * 10
                    side = int(e.get('SIDE', '0'))
                    pt = [(x, y - d), (x + xs, y - d), (x + d, y), (x + d, y - ys)][side]
                    sym['entries'].append((e.get('NAME', ''), pt))
                self.symbols.append(sym)

    def local_nets(self, dsu):
        """Union geometric connectivity on this sheet; node ids are (sid, kind, idx)."""
        S = self.sid
        segs = []  # (a, b, node, is_bus)
        for k, pts in enumerate(self.wires):
            for a, b in zip(pts, pts[1:]): segs.append((a, b, (S, 'w', k), False))
        for k, pts in enumerate(self.buses):
            for a, b in zip(pts, pts[1:]): segs.append((a, b, (S, 'b', k), True))

        def attach(node, pt, bus=False, interior_ok=True):
            for a, b, seg, is_bus in segs:
                if is_bus != bus: continue
                if pt == a or pt == b or (interior_ok and on_segment(pt, a, b)):
                    dsu.union(node, seg)
        # wire/bus endpoints touching other segments (T-connections) and junctions
        for k, pts in enumerate(self.wires):
            for pt in (pts[0], pts[-1]): attach((S, 'w', k), pt)
        for k, pts in enumerate(self.buses):
            for pt in (pts[0], pts[-1]): attach((S, 'b', k), pt, bus=True)
        for j, pt in enumerate(self.junctions): attach((S, 'j', j), pt)
        by_point = collections.defaultdict(list)
        for k, p in enumerate(self.pins):
            node = (S, 'pin', k); dsu.find(node); attach(node, p['pt']); by_point[p['pt']].append(node)
        for pts in by_point.values():  # pin touching pin directly
            for n in pts[1:]: dsu.union(pts[0], n)
        pin_at = collections.defaultdict(list)
        for k, p in enumerate(self.pins): pin_at[p['pt']].append((S, 'pin', k))
        def touch_pins(node, pt):
            for n in pin_at.get(pt, ()): dsu.union(node, n)
        for k, (name, pt) in enumerate(self.labels):
            attach((S, 'lbl', k), pt, bus=bool(expand_bus(name)))
            if not expand_bus(name): touch_pins((S, 'lbl', k), pt)
        for k, (name, ends) in enumerate(self.ports):
            if not expand_bus(name):
                for pt in ends: touch_pins((S, 'port', k), pt)
        for j, pt in enumerate(self.junctions): touch_pins((S, 'j', j), pt)
        for si, sym in enumerate(self.symbols):
            for ei, (name, pt) in enumerate(sym['entries']):
                if not expand_bus(name): touch_pins((S, 'ent', si, ei), pt)
        for k, (name, pt) in enumerate(self.power):
            attach((S, 'pwr', k), pt)
            for pk, p in enumerate(self.pins):
                if p['pt'] == pt: dsu.union((S, 'pwr', k), (S, 'pin', pk))
        for k, (name, ends) in enumerate(self.ports):
            for pt in ends: attach((S, 'port', k), pt, bus=bool(expand_bus(name)), interior_ok=False)
        for si, sym in enumerate(self.symbols):
            for ei, (name, pt) in enumerate(sym['entries']):
                attach((S, 'ent', si, ei), pt, bus=bool(expand_bus(name)), interior_ok=False)


def build(prj):
    root = os.path.dirname(prj)
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read_string(open(prj, 'rb').read().decode('utf-8-sig', errors='replace'))
    docs = [cp[s]['DocumentPath'] for s in cp.sections() if s.startswith('Document') and cp[s].get('DocumentPath', '').lower().endswith('.schdoc')]
    sheets = {d: Sheet(os.path.join(root, d), d) for d in docs}
    dsu = DSU()
    for sh in sheets.values(): sh.local_nets(dsu)

    # sheet-local net labels: same name on same sheet => same net
    # bus labels/ports: member nets are the sheet-local nets with the member name
    for sh in sheets.values():
        first = {}
        for k, (name, _) in enumerate(sh.labels):
            if expand_bus(name): continue
            node = (sh.sid, 'lbl', k)
            if name in first: dsu.union(node, first[name])
            else: first[name] = node
        sh.label_node = first
    # power ports are global
    first = {}
    for sh in sheets.values():
        for k, (name, _) in enumerate(sh.power):
            node = (sh.sid, 'pwr', k)
            if name in first: dsu.union(node, first[name])
            else: first[name] = node

    def member_node(sheet, member):
        return sheet.label_node.get(member)

    # hierarchy: sheet entry on parent <-> port with same name on child
    for parent in sheets.values():
        for si, sym in enumerate(parent.symbols):
            child = sheets.get(sym['file'])
            if not child: continue
            for ei, (name, _) in enumerate(sym['entries']):
                ent = (parent.sid, 'ent', si, ei)
                for pk, (pname, _) in enumerate(child.ports):
                    if pname != name: continue
                    members = expand_bus(name)
                    if not members:
                        dsu.union(ent, (child.sid, 'port', pk))
                        continue
                    # bus: entry node stands for the bus on the parent; connect members by name
                    for m in members:
                        c = member_node(child, m)
                        if c: dsu.union((parent.sid, 'busmember', dsu.find(ent), m), c)

    # sheet-local naming + collect nets
    nets = collections.defaultdict(lambda: dict(names=set(), pins=[]))
    for sh in sheets.values():
        for k, p in enumerate(sh.pins):
            nets[dsu.find((sh.sid, 'pin', k))]['pins'].append(p)
        for k, (name, _) in enumerate(sh.labels):
            if not expand_bus(name): nets[dsu.find((sh.sid, 'lbl', k))]['names'].add(name)
        for k, (name, _) in enumerate(sh.power):
            nets[dsu.find((sh.sid, 'pwr', k))]['names'].add(name)
        for k, (name, _) in enumerate(sh.ports):
            if not expand_bus(name): nets[dsu.find((sh.sid, 'port', k))]['names'].add(name)
    out = []
    for n in nets.values():
        if not n['pins']: continue
        name = sorted(n['names'], key=lambda s: (len(s), s))[0] if n['names'] else \
            'Net%s_%s' % (n['pins'][0]['des'], n['pins'][0]['num'])
        out.append(dict(name=name, aliases=sorted(n['names']), pins=n['pins']))
    return sheets, out


if __name__ == '__main__':
    sheets, nets = build(sys.argv[1])
    npins = sum(len(s.pins) for s in sheets.values())
    print(f"{len(sheets)} sheets, {npins} placed pins, {len(nets)} nets "
          f"({sum(1 for n in nets if len(n['pins']) == 1)} single-pin)")
    for q in sys.argv[2:]:
        hits = [(n, p) for n in nets for p in n['pins'] if p['name'] == q or f"{p['des']}.{p['num']}" == q or n['name'] == q]
        seen = set()
        for n, p in hits:
            if id(n) in seen: continue
            seen.add(id(n))
            others = ', '.join(f"{x['des']}.{x['num']}({x['name']})" for x in n['pins'])
            alias = f" aka {n['aliases']}" if len(n['aliases']) > 1 else ''
            print(f"\n{q}: net {n['name']}{alias}\n   -> {others}")
