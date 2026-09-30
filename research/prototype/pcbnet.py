"""Prototype: read pad->net assignments from an Altium .PcbDoc (layout of the same board)."""
import struct, collections, olefile

def text_records(data):
    out, i = [], 0
    while i + 4 <= len(data):
        (n,) = struct.unpack_from('<I', data, i); i += 4
        t = data[i:i+n].rstrip(b'\0').decode('latin-1'); i += n
        d = {}
        for part in t.split('|'):
            if '=' in part:
                k, v = part.split('=', 1); d[k.upper()] = v
        out.append(d)
    return out

def pcb_netlist(path):
    ole = olefile.OleFileIO(path)
    nets = [r.get('NAME') for r in text_records(ole.openstream('Nets6/Data').read())]
    comps = [r.get('SOURCEDESIGNATOR') for r in text_records(ole.openstream('Components6/Data').read())]
    data = ole.openstream('Pads6/Data').read()
    i, pads = 0, []
    while i < len(data):
        rtype = data[i]; i += 1
        assert rtype == 2, rtype
        (n1,) = struct.unpack_from('<I', data, i); i += 4
        slen = data[i]; name = data[i+1:i+1+slen].decode('latin-1'); i += n1
        for _ in range(3):
            (n,) = struct.unpack_from('<I', data, i); i += 4 + n
        (n5,) = struct.unpack_from('<I', data, i); i += 4
        net, comp = struct.unpack_from('<H2xH', data, i + 3)
        i += n5
        # optional subrecord 6 (size/shape table) follows when present
        if i < len(data) and data[i] != 2:
            (n6,) = struct.unpack_from('<I', data, i); i += 4 + n6
        if comp != 0xFFFF and net != 0xFFFF:
            pads.append((comps[comp], name, nets[net]))
    by_net = collections.defaultdict(set)
    for des, pin, net in pads: by_net[net].add((des, pin))
    return by_net

if __name__ == '__main__':
    import sys
    by_net = pcb_netlist(sys.argv[1])
    print(len(by_net), 'nets with component pads;', sum(len(v) for v in by_net.values()), 'pad-net assignments')
