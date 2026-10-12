#!/usr/bin/env python3
"""Toyota Techstream database (your own copy of the install folder) -> Data List catalog of one vehicle:
ECUs with their CAN addresses, Data List parameters with scaling, and DTC descriptions.

  ddb2cat.py models   "<Techstream folder>" [--region JP] [--model PAS]
  ddb2cat.py vehicles "<Techstream folder>" [--region JP] [--model PASSO]
  ddb2cat.py build    "<Techstream folder>" --vehicle 6A9D [--region JP] [-o catalog.txt]

models   - models that have ECUs in the database: variants per region, how many of them include UDS ECUs.
vehicles - vehicles: region, id, model, ECUs with marks (see the legend); variants differ in their ECU sets.
build    - catalog of one vehicle, to stdout without -o. The region defaults to the only one that has it.

Two kinds of ECUs: KWP on CAN (requests 21 LID, Techstream "P4" database) and UDS (22 DID, "P5" - newer
vehicles). UDS ECUs and ECUs behind the gateway (extended addressing) are not verified on a real vehicle yet.

Catalog: UTF-8 text, one record per line, fields separated by "|" ("|" inside values is replaced by "/"):
  #catalog|2|vehicle=6A9D|region=JP|model=...   format version
  B|id|name|address|probes|protocol            ECU: CAN id of requests (hex, replies come from id + 8); behind
                                               the gateway "750/xx" (xx - extended address, the first byte of
                                               every frame); empty - unknown: find it by sending the probes
                                               (requests of the rarest LIDs/DIDs); protocol KWP (DTC 13 80)
                                               or UDS (19 02)
  L|LID|N                                      full length of the reply to 21 LID (without 61 LID); KWP only
  P|section|name|database name|request|bit1|bit2|signed|mul|div|add|dec|unit|values
                                               parameter: request 21 LID or 22 DID; bits bit1..bit2 of the full
                                               reply counted from the MSB of the first byte after 61 LID /
                                               62 DID; signed 1 - two's complement over the field width;
                                               value = (raw*mul/div + add)/10^dec or a label "raw=label;...";
                                               section and database name are empty: they are for tools that
                                               group and rename parameters (keeping the original name there)
  D|code|description                           DTC; UDS codes have 7 characters: code + failure type (P055512)

See README.md for the database format.
"""

import argparse
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = 2
KIND = {1: 'enum', 2: 'num', 3: 'hex', 4: 'ascii', 5: 'dtc'}
SERVICE = {0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0, 0xE1, 0xE2}  # service LIDs present in every ECU
PROBES = 3
MARKS = '* - UDS, ^ - behind the gateway (extended addressing): not verified on a real vehicle yet; ' \
        '? - no CAN address in the database'


def lzss(src, pos, size):
    """Okumura LZSS: one flag bit per item (1 - literal byte, 0 - 12-bit position + 4-bit length - 3)."""
    buf, r, out, flags = bytearray(4096), 0xFEE, bytearray(), 0
    while len(out) < size:
        flags >>= 1
        if not flags & 0x100:
            flags = src[pos] | 0xFF00
            pos += 1
        if flags & 1:
            i, n = None, 1
        else:
            i, n = src[pos] | (src[pos + 1] & 0xF0) << 4, (src[pos + 1] & 0x0F) + 3
            pos += 1
        for k in range(n):  # byte by byte: a reference may overlap what is being written
            c = src[pos] if i is None else buf[(i + k) & 0xFFF]
            out.append(c)
            buf[r] = c
            r = (r + 1) & 0xFFF
        pos += 1
    return bytes(out[:size])


def find(b, t):
    """Offset of the section of type t and its type with the compression flag; (None, 0) - no such section."""
    offs, p, end = [], 0x24, None
    while end is None or p < end:
        x, = struct.unpack_from('<I', b, p)
        p += 4
        if x:
            offs.append(x)
            end = x if end is None else min(end, x)
    return next(((s, typ) for s in offs for typ in struct.unpack_from('<H', b, s) if typ & 0xFF == t), (None, 0))


def section(b, t):
    """Section of type t: (record count, data); a compressed one is unpacked."""
    s, typ = find(b, t)
    if s is None:
        return 0, b''
    n, = struct.unpack_from('<I', b, s + 2)
    if typ >> 8:
        return n, lzss(b, s + 15, struct.unpack_from('<I', b, s + 10)[0])
    return n, b[s + 10:s + 10 + struct.unpack_from('<I', b, s + 6)[0]]


def records(b, t):
    n, data = section(b, t)
    ln = len(data) // n if n else 0
    return [data[ln * i:ln * (i + 1)] for i in range(n)]


def strings(path):
    """String table (M_English.ddb): string id = index + 1."""
    n, raw = section(path.read_bytes(), 0)
    out = []
    for i in range(n):
        off, ln = struct.unpack_from('<IH', raw, i * 6)
        out.append(raw[n * 6 + off:n * 6 + off + ln].decode('utf-16le', 'replace').rstrip('\0'))
    return out


def text(M, i):
    return M[i - 1] if 0 < i <= len(M) else ''


@dataclass
class Param:
    name: str
    sid: int  # 0x21 - KWP (LID), 0x22 - UDS (DID)
    lid: int  # LID or DID
    bs: int
    be: int
    kind: str
    hidden: bool  # Techstream does not show it in the Data List
    ffd: int  # 1 - Data List, 2 - freeze frame only, 3 - both
    datalist: bool  # shown in the Data List
    signed: bool = False
    mul: int = 1
    div: int = 1
    add: int = 0
    dec: int = 0
    unit: str = ''
    enum: dict = field(default_factory=dict)

    @property
    def req(self):
        return f'{self.sid:02X}{self.lid:0{2 if self.sid == 0x21 else 4}X}'


@dataclass
class Ecu:
    file: str
    title: str
    lids: dict  # LID -> full reply length (KWP)
    params: list
    dtc: list
    uds: bool


def is_uds(b):
    """UDS ECU ("P5" database): parameters in section 0x3E instead of 0x0A."""
    return find(b, 0x0A)[0] is None and find(b, 0x3E)[0] is not None


def load_ecu(path, M, title):
    b = path.read_bytes()
    uds = is_uds(b)
    units = {struct.unpack_from('<H', r, 4)[0]: text(M, struct.unpack_from('<I', r, 0)[0]) for r in records(b, 0x0F)}
    conv = {}
    for r in records(b, 0x0D):
        mul, div, add, cid, unit, _, sg, dec = struct.unpack_from('<IIiHHIBB', r)
        conv[cid] = (bool(sg), mul, div, add, dec, units.get(unit, ''))
    enums = defaultdict(dict)
    for r in records(b, 0x0E):
        nid, val, _, eid = struct.unpack_from('<IIIH', r)
        enums[eid][val] = text(M, nid)
    params = []
    for r in records(b, 0x3E) if uds else records(b, 0x0A):
        if uds:
            nid, _, flags, idx = struct.unpack_from('<IIIH', r, 0x18)
            cid, bs, be, _, eid, _, lid, _, kind = struct.unpack_from('<9H', r, 0x2A)
            sid, ffd, datalist = 0x22, flags & 3, bool(flags & 1)
        else:
            nid, _, idx, cid, bs, be, _, _, eid, _, kind, ffd, lid = struct.unpack_from('<IIIHHHHHHIHBB', r, 48)
            sid, datalist = 0x21, ffd != 2
        p = Param(text(M, nid) or f'#{idx}', sid, lid, bs, be, KIND.get(kind & 0xFF, str(kind & 0xFF)),
                  bool(kind & 0x200), ffd, datalist)
        if p.kind == 'num':
            p.signed, p.mul, p.div, p.add, p.dec, p.unit = conv.get(cid, (False, 1, 1, 0, 0, ''))
        elif p.kind == 'enum':
            p.enum = enums.get(eid, {})
        params.append(p)
    params.sort(key=lambda p: (p.lid, p.bs, p.be))
    dtc = []
    if uds:
        for r in records(b, 0x41):
            name, fault = (text(M, i) for i in struct.unpack_from('<II', r, 0x30))
            dtc.append((r[:44].decode('utf-16le', 'replace').split('\0')[0], name + (f': {fault}' if fault else '')))
    for r in records(b, 0x05):
        code = r[:12].decode('utf-16le', 'replace').split('\0')[0]
        dtc.append((code, text(M, struct.unpack_from('<I', r, 12)[0])))
    return Ecu(path.name, title, {r[2]: r[3] for r in records(b, 0x06)}, params, dtc, uds)


@dataclass
class Db:
    region: str
    path: Path
    M: list
    models: dict  # vehicle -> model
    ecus: dict  # vehicle -> [(.ddb file, ECU title)] without duplicates
    addr: dict  # .ddb file -> CAN address ("7B0", behind the gateway "750/40"), '' - none (K-line, Ethernet)
    uds: set  # .ddb files of UDS ECUs

    def marks(self, f):
        """ECU marks in listings: * - UDS, ^ - behind the gateway, ? - no CAN address."""
        a = self.addr.get(f, '')
        return '*' * (f in self.uds) + '^' * ('/' in a) + '?' * (not a)


def load_db(path, region=''):
    """Database folder (<Techstream>/<region>/DB)."""
    M = strings(path / 'M_English.ddb')
    b = (path / 'Toyota.ddb').read_bytes()
    models = {vid: text(M, nid) for nid, vid, _ in (struct.unpack('<3I', r) for r in records(b, 0x2B))}
    can = {}
    for r in sorted(records(b, 0x0D), key=lambda r: struct.unpack_from('<H', r, 0x18)[0]):  # normal addressing first
        eid, req, ext, bus = (struct.unpack_from('<H', r, o)[0] for o in (0, 8, 0xA, 0x18))
        if bus in (0x12, 0x22) and 0x700 <= req <= 0x7FF and eid not in can:
            can[eid] = f'{req:03X}' + (f'/{ext:02X}' if bus == 0x22 else '')
    files, addr = {}, {}
    for r in records(b, 0x10):
        nid, _, eid = struct.unpack_from('<III', r, 60)
        name = r[:60].decode('utf-16le').split('\0')[0]
        files[eid] = (name, text(M, nid) or Path(name).stem)  # some ECUs have no title in the database
        addr[name] = can.get(eid, '')
    ecus = defaultdict(list)
    for r in records(b, 0x17):
        ecu, _, _, _, veh = struct.unpack_from('<5I', r)
        f = files.get(ecu & 0xFFFF)
        if f and f not in ecus[veh]:
            ecus[veh].append(f)
    used = {f for fs in ecus.values() for f, _ in fs}
    return Db(region, path, M, models, dict(ecus), addr, {f for f in used if is_uds((path / f).read_bytes())})


def databases(root, region):
    names = [region] if region else sorted(p.parent.parent.name for p in root.glob('*/DB/Toyota.ddb'))
    if not names:
        raise SystemExit(f'no */DB/Toyota.ddb in {root} - point to the Techstream install folder')
    return (load_db(root / r / 'DB', r) for r in names)


def clean(s):
    return ' '.join(str(s).replace('|', '/').split())  # line breaks (and \r) become spaces


def catalog(db, vehicle):
    """Catalog lines of the vehicle."""
    ecus = [load_ecu(db.path / f, db.M, t) for f, t in db.ecus.get(vehicle, [])]
    shown = lambda e: [p for p in e.params if p.datalist and not p.hidden and p.kind in ('num', 'enum')]
    freq = Counter(req for e in ecus for req in {p.req for p in shown(e)})
    out = [f'#catalog|{FORMAT}|vehicle={vehicle:X}|region={db.region}|model={clean(db.models.get(vehicle, ""))}']
    for e in ecus:
        bid = Path(e.file).stem
        params = shown(e)
        probes = sorted({p.req for p in params if p.sid != 0x21 or p.lid not in SERVICE}, key=lambda x: (freq[x], x))
        out.append('|'.join(['B', bid, clean(e.title[:1].upper() + e.title[1:]), db.addr[e.file],
                             ','.join(probes[:PROBES]), 'UDS' if e.uds else 'KWP']))
        out += [f'L|{lid:02X}|{n}' for lid, n in sorted(e.lids.items())]
        out += dict.fromkeys('|'.join(map(clean, [  # in reply order; the database has duplicates
            'P', '', p.name, '', p.req, p.bs, p.be, int(p.signed), p.mul, p.div, p.add, p.dec, p.unit,
            ';'.join(f'{k}={v}' for k, v in sorted(p.enum.items()))])) for p in params)
        out += [f'D|{clean(c)}|{clean(d)}' for c, d in e.dtc]
    return out


def cmd_models(a):
    found = defaultdict(lambda: defaultdict(Counter))  # model -> region -> {all, uds: variant count}
    for db in databases(a.techstream, a.region):
        for vid, model in db.models.items():
            model = model.upper()  # NA has "Corolla", JP and EU "COROLLA"
            if db.ecus.get(vid) and (not a.model or a.model.upper() in model):
                found[model][db.region]['all'] += 1
                found[model][db.region]['uds'] += any(f in db.uds for f, _ in db.ecus[vid])
    print(f'{"model":<32} region: variants (with UDS ECUs - not verified on a real vehicle yet)')
    for model in sorted(found):
        print(f'{model:<32} ' + '  '.join(f'{r} {c["all"]}' + (f' (UDS {c["uds"]})' if c['uds'] else '')
                                          for r, c in sorted(found[model].items())))


def cmd_vehicles(a):
    for db in databases(a.techstream, a.region):
        for vid, model in sorted(db.models.items(), key=lambda x: (x[1], x[0])):
            if a.model and a.model.lower() not in model.lower():
                continue
            files = [Path(f).stem + db.marks(f) for f, _ in db.ecus.get(vid, [])]
            print(f'{db.region:<4} {vid:04X}  {model:<32} {len(files):>2}: {" ".join(files)}')
    print(MARKS)


def cmd_build(a):
    found = [db for db in databases(a.techstream, a.region) if db.ecus.get(a.vehicle)]
    if len(found) != 1:
        where = ', '.join(db.region for db in found)
        raise SystemExit(f'vehicle {a.vehicle:X} ' + (f'is in regions {where} - choose one with --region' if found
                                                      else 'not found (see ddb2cat.py vehicles)'))
    db = found[0]
    marked = [Path(f).stem + db.marks(f) for f, _ in db.ecus[a.vehicle] if db.marks(f)]
    if marked:
        print(f'{" ".join(marked)}\n  {MARKS}', file=sys.stderr)
    out = catalog(db, a.vehicle)
    body = '\n'.join(out) + '\n'
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(body, encoding='utf-8')
    else:
        sys.stdout.write(body)
    count = lambda k: sum(ln.startswith(k) for ln in out)
    print(f'{a.output or "stdout"}: {count("B|")} ECUs, {count("P|")} parameters, {count("D|")} DTCs',
          file=sys.stderr)


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('models', help='models in the database')
    m.add_argument('techstream', type=Path)
    m.add_argument('--region')
    m.add_argument('--model', help='part of the model name')
    m.set_defaults(run=cmd_models)
    v = sub.add_parser('vehicles', help='vehicles in the database')
    v.add_argument('techstream', type=Path)
    v.add_argument('--region')
    v.add_argument('--model', help='part of the model name')
    v.set_defaults(run=cmd_vehicles)
    b = sub.add_parser('build', help='catalog of one vehicle')
    b.add_argument('techstream', type=Path)
    b.add_argument('--vehicle', required=True, type=lambda s: int(s, 16), help='vehicle id (hex)')
    b.add_argument('--region')
    b.add_argument('-o', '--output', type=Path)
    b.set_defaults(run=cmd_build)
    a = a.parse_args(argv)
    a.run(a)


if __name__ == '__main__':
    main()
