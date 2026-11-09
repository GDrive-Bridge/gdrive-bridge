"""Tiny QR code encoder (byte mode, error correction M, versions 1-10) and PNG writer."""
import struct
import zlib

# version -> (EC codewords per block, [(blocks, data codewords per block), ...])
_EC_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_REMAINDER = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_ec(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _gf_mul(g, _EXP[i])
        gen = nxt
    rem = [0] * n
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


def _bch(value, poly, bits):
    v = value << bits
    plen = poly.bit_length()
    while v.bit_length() >= plen:
        v ^= poly << (v.bit_length() - plen)
    return (value << bits) | v


def _codewords(data):
    for version in range(1, 11):
        ec_len, groups = _EC_M[version]
        capacity = sum(b * d for b, d in groups)
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= capacity * 8:
            break
    else:
        raise ValueError('text too long for this QR encoder')
    bits = [0, 1, 0, 0]
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, k = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[k:k + size])
            k += size
    ecs = [_rs_ec(b, ec_len) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        out += [e[i] for e in ecs]
    return version, out


def _penalty(m):
    n = len(m)
    score = 0
    for grid in (m, [list(col) for col in zip(*m)]):
        for row in grid:
            run, prev = 0, None
            for v in row:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            s = ''.join('1' if v else '0' for v in row)
            score += 40 * (s.count('10111010000') + s.count('00001011101'))
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(r) for r in m)
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score


_MASKS = [
    lambda y, x: (x + y) % 2 == 0, lambda y, x: y % 2 == 0, lambda y, x: x % 3 == 0,
    lambda y, x: (x + y) % 3 == 0, lambda y, x: (y // 2 + x // 3) % 2 == 0,
    lambda y, x: (x * y) % 2 + (x * y) % 3 == 0, lambda y, x: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda y, x: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
]


def encode(text, force_mask=None):
    """Return the QR matrix (list of rows of 0/1) for `text`."""
    version, words = _codewords(text.encode('utf-8'))
    n = 17 + 4 * version
    mod = [[0] * n for _ in range(n)]
    func = [[False] * n for _ in range(n)]

    def setf(x, y, dark):
        mod[y][x] = 1 if dark else 0
        func[y][x] = True

    for i in range(n):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    d = max(abs(dx), abs(dy))
                    setf(x, y, d not in (2, 4))
    pos = _ALIGN[version]
    for i, ay in enumerate(pos):
        for j, ax in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or \
                    (i == len(pos) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    # reserve format areas (filled per mask below) and the dark module
    for i in range(9):
        func[8][i] = func[i][8] = True
    for i in range(8):
        func[8][n - 1 - i] = func[n - 1 - i][8] = True
    setf(8, n - 8, True)
    if version >= 7:
        vbits = _bch(version, 0x1F25, 12)
        for i in range(18):
            bit = (vbits >> i) & 1
            a, b = n - 11 + i % 3, i // 3
            setf(a, b, bit)
            setf(b, a, bit)
    # data, zigzag from the bottom right
    bits = []
    for w in words:
        bits += [(w >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * _REMAINDER[version]
    k = 0
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = n - 1 - vert if upward else vert
                if not func[y][x] and k < len(bits):
                    mod[y][x] = bits[k]
                    k += 1
        right -= 2

    best = None
    for mask in (range(8) if force_mask is None else [force_mask]):
        m = [row[:] for row in mod]
        f = _MASKS[mask]
        for y in range(n):
            for x in range(n):
                if not func[y][x] and f(y, x):
                    m[y][x] ^= 1
        fbits = _bch((0 << 3) | mask, 0x537, 10) ^ 0x5412   # level M = 00
        for i in range(6):
            m[i][8] = (fbits >> i) & 1
        m[7][8] = (fbits >> 6) & 1
        m[8][8] = (fbits >> 7) & 1
        m[8][7] = (fbits >> 8) & 1
        for i in range(9, 15):
            m[8][14 - i] = (fbits >> i) & 1
        for i in range(8):
            m[8][n - 1 - i] = (fbits >> i) & 1
        for i in range(8, 15):
            m[n - 15 + i][8] = (fbits >> i) & 1
        m[n - 8][8] = 1
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def write_png(path, width, height, pixel):
    """Write an RGBA PNG; pixel(x, y) returns (r, g, b, a)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 9)))
        f.write(chunk(b'IEND', b''))


def save_qr(text, path, scale=10, border=4):
    m = encode(text)
    size = (len(m) + 2 * border) * scale
    dark, light = (0, 0, 0, 255), (255, 255, 255, 255)

    def pixel(x, y):
        mx, my = x // scale - border, y // scale - border
        if 0 <= mx < len(m) and 0 <= my < len(m) and m[my][mx]:
            return dark
        return light

    write_png(path, size, size, pixel)
