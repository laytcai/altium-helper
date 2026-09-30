import sys, struct, collections, olefile

def read_records(path):
    ole = olefile.OleFileIO(path)
    data = ole.openstream('FileHeader').read()
    recs, i = [], 0
    while i + 4 <= len(data):
        (hdr,) = struct.unpack_from('<I', data, i); i += 4
        length, rtype = hdr & 0xFFFFFF, hdr >> 24
        chunk = data[i:i+length]; i += length
        if rtype != 0:          # binary record (rare in FileHeader)
            recs.append({'_binary': True}); continue
        text = chunk.rstrip(b'\0').decode('latin-1')
        d = {}
        for part in text.split('|'):
            if '=' in part:
                k, v = part.split('=', 1); d[k.upper()] = v
        recs.append(d)
    return ole, recs

if __name__ == '__main__':
    for p in sys.argv[1:]:
        ole, recs = read_records(p)
        streams = ['/'.join(s) for s in ole.listdir()]
        c = collections.Counter(r.get('RECORD', 'HDR') for r in recs)
        print(f"{p}: {len(recs)} records, streams={streams}")
        print("   record types:", dict(sorted(c.items(), key=lambda kv: -kv[1])))
