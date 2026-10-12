"""Synthetic Techstream-like database: checks the format parsing end to end without a real Techstream copy."""

import io
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ddb2cat  # noqa: E402

STRINGS = ['TESTCAR', 'Engine', 'Engine UDS', 'Body', 'Old', 'Engine Speed', 'rpm', 'MIL', 'OFF', 'ON', 'Misfire',
           'Circuit Open', 'Hidden', 'Freeze only', 'Door']


def sid(s):
    return STRINGS.index(s) + 1


def literal_lzss(data):
    """LZSS stream of literals only: a flag byte 0xFF before every 8 bytes."""
    return b''.join(b'\xff' + data[i:i + 8] for i in range(0, len(data), 8))


def ddb(sections):
    """{type: (count, data, compressed)} -> .ddb bytes: header, section offsets from 0x24, sections."""
    pos = 0x24 + 4 * len(sections)
    offsets, body = [], bytearray()
    for t, (count, data, packed) in sections.items():
        offsets.append(pos + len(body))
        if packed:
            z = literal_lzss(data)
            body += struct.pack('<HIIIB', t | 0x100, count, len(z), len(data), 0) + z
        else:
            body += struct.pack('<HII', t, count, len(data)) + data
    return b'DiagTool DataCtrl'.ljust(0x24, b'\0') + b''.join(struct.pack('<I', o) for o in offsets) + bytes(body)


def recs(*rs, packed=False):
    return len(rs), b''.join(rs), packed


def record(size, *fields):
    """Record of the given size with (offset, format, values...) fields."""
    r = bytearray(size)
    for off, fmt, *vals in fields:
        struct.pack_into(fmt, r, off, *vals)
    return bytes(r)


def utf16(s, size):
    return s.encode('utf-16le').ljust(size, b'\0')


def common():
    """Formulas, enums and units shared by both ECU kinds."""
    return {
        0x0D: recs(record(24, (0, '<IIiHHIBB', 1, 4, 0, 1, 1, 0, 0, 0))),  # formula 1: x/4 rpm
        0x0E: recs(record(24, (0, '<IIIH', sid('OFF'), 0, 0, 1)), record(24, (0, '<IIIH', sid('ON'), 1, 0, 1))),
        0x0F: recs(record(12, (0, '<IH', sid('rpm'), 1))),
    }


def kwp_param(name, lid, bs, be, kind, ffd, cid=0, eid=0):
    return record(84, (48, '<IIIHHHHHHIHBB', sid(name), 0, 1, cid, bs, be, 0, 0, eid, 0, kind, ffd, lid))


def uds_param(name, did, bs, be, kind, flags, cid=0, eid=0):
    return record(64, (0x18, '<IIIH', sid(name), 0, flags, 1), (0x2A, '<9H', cid, bs, be, 0, eid, 0, did, 0, kind))


def build_db(root):
    db = root / 'JP' / 'DB'
    db.mkdir(parents=True)
    table, blob = bytearray(), bytearray()
    for s in STRINGS:
        e = s.encode('utf-16le')
        table += struct.pack('<IH', len(blob), len(e))
        blob += e
    (db / 'M_English.ddb').write_bytes(ddb({0: (len(STRINGS), bytes(table + blob), True)}))
    ecus = [('ENG_P4.ddb', 'Engine', 1, '7E0', 0, 0x12), ('ENG_P5.ddb', 'Engine UDS', 2, '700', 0, 0x12),
            ('BODY_P4.ddb', 'Body', 3, '750', 0x40, 0x22), ('OLD_P3.ddb', 'Old', 4, None, 0, 0)]
    (db / 'Toyota.ddb').write_bytes(ddb({
        0x2B: recs(struct.pack('<3I', sid('TESTCAR'), 0x6A9D, 0)),
        0x10: recs(*(utf16(f, 60) + struct.pack('<4I', sid(t), 0, eid, 4) for f, t, eid, *_ in ecus)),
        0x17: recs(*(struct.pack('<5I', eid, 0, 0, 0, 0x6A9D) for _, _, eid, *_ in ecus)),
        0x0D: recs(*(record(28, (0, '<H', eid), (8, '<HH', int(a, 16), ext), (0x18, '<H', bus))
                     for _, _, eid, a, ext, bus in ecus if a)),
    }))
    (db / 'ENG_P4.ddb').write_bytes(ddb({
        **common(),
        0x0A: recs(kwp_param('Engine Speed', 0x01, 0, 15, 2, 1, cid=1), kwp_param('MIL', 0x01, 16, 16, 1, 3, eid=1),
                   kwp_param('Hidden', 0x01, 17, 17, 0x201, 1, eid=1), kwp_param('Freeze only', 0x02, 0, 7, 2, 2),
                   packed=True),
        0x06: recs(bytes([0, 0, 0x01, 4, 0, 0, 0, 0])),
        0x05: recs(utf16('P0300', 12) + struct.pack('<I', sid('Misfire')).ljust(16, b'\0')),
    }))
    (db / 'ENG_P5.ddb').write_bytes(ddb({
        **common(),
        0x3D: recs(struct.pack('<4H', 0, 0x1F0C, 0, 0), struct.pack('<4H', 0, 0x1F01, 0, 0)),
        0x3E: recs(uds_param('Engine Speed', 0x1F0C, 0, 15, 2, 0x43, cid=1),
                   uds_param('MIL', 0x1F01, 0, 0, 1, 0x41, eid=1), uds_param('Freeze only', 0x1F02, 0, 7, 2, 0x42),
                   uds_param('MIL', 0x1F01, 0, 0, 1, 0x41, eid=1)),  # the database has duplicates
        0x41: recs(record(68, (0, '44s', utf16('P030000', 44)),
                          (0x2C, '<III', 0x030000, sid('Misfire'), sid('Circuit Open')))),
    }))
    (db / 'BODY_P4.ddb').write_bytes(ddb({0x0A: recs(kwp_param('Door', 0x10, 0, 0, 1, 1, eid=1))}))
    old_dtc = utf16('B1000', 12) + struct.pack('<I', sid('Door')).ljust(16, b'\0')
    (db / 'OLD_P3.ddb').write_bytes(ddb({0x05: recs(old_dtc)}))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        ddb2cat.main([str(a) for a in argv])
    return out.getvalue()


class Ddb2CatTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        build_db(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_lzss_back_reference(self):
        # literals A, B, C, then a 6-byte reference to the window start that overlaps what is being written
        self.assertEqual(ddb2cat.lzss(bytes([0x07]) + b'ABC' + bytes([0xEE, 0xF3]), 0, 9), b'ABCABCABC')

    def test_models(self):
        self.assertIn('TESTCAR                          JP 1 (UDS 1)', run('models', self.root))

    def test_vehicles_marks(self):
        line = run('vehicles', self.root).splitlines()[0]
        self.assertEqual(line, 'JP   6A9D  TESTCAR                           4: ENG_P4 ENG_P5* BODY_P4^ OLD_P3?')

    def test_build(self):
        out = self.root / 'catalog.txt'
        run('build', self.root, '--vehicle', '6A9D', '-o', out)
        self.assertEqual(out.read_text(encoding='utf-8').splitlines(), [
            '#catalog|2|vehicle=6A9D|region=JP|model=TESTCAR',
            'B|ENG_P4|Engine|7E0|2101|KWP',
            'L|01|4',
            'P||Engine Speed||2101|0|15|0|1|4|0|0|rpm|',
            'P||MIL||2101|16|16|0|1|1|0|0||0=OFF;1=ON',
            'D|P0300|Misfire',
            'B|ENG_P5|Engine UDS|700|221F01,221F0C|UDS',
            'P||MIL||221F01|0|0|0|1|1|0|0||0=OFF;1=ON',
            'P||Engine Speed||221F0C|0|15|0|1|4|0|0|rpm|',
            'D|P030000|Misfire: Circuit Open',
            'B|BODY_P4|Body|750/40|2110|KWP',
            'P||Door||2110|0|0|0|1|1|0|0||',
            'B|OLD_P3|Old|||KWP',
            'D|B1000|Door',
        ])


if __name__ == '__main__':
    unittest.main()
