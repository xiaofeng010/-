# -*- coding: utf-8 -*-
"""
pyqr.py —— 零依赖 QR Code 编码器（仅使用 Python 标准库）

用途：在批处理/命令行里输出可用于手机扫描的二维码（字节模式，纠错等级 M，
版本 1~6 自动选择，容量 14~134 字节，足够容纳网易云登录链接）。

为什么自己实现：目标机器上没有任何第三方库（无 qrcode / Pillow），
而且 pip 安装不稳定，所以这里按 ISO/IEC 18004 标准自行实现，保证开箱即用。
"""

__all__ = ["make_matrix", "render_lines", "qr_lines", "capacity_bytes"]

# ---------------------------------------------------------------- 规格表
# 版本 -> (每块纠错码字数, [(块数, 数据码字数), ...])
ECC_TABLE = {
    1:  {0: (7,  [(1, 19)]),
         1: (10, [(1, 16)]),
         2: (13, [(1, 13)]),
         3: (17, [(1, 9)])},
    2:  {0: (10, [(1, 34)]),
         1: (16, [(1, 28)]),
         2: (22, [(1, 22)]),
         3: (28, [(1, 16)])},
    3:  {0: (15, [(1, 55)]),
         1: (26, [(1, 44)]),
         2: (18, [(2, 17)]),
         3: (22, [(2, 13)])},
    4:  {0: (20, [(1, 80)]),
         1: (18, [(2, 32)]),
         2: (26, [(2, 24)]),
         3: (16, [(4, 9)])},
    5:  {0: (26, [(1, 108)]),
         1: (24, [(2, 43)]),
         2: (18, [(2, 15), (2, 16)]),
         3: (22, [(2, 11), (2, 12)])},
    6:  {0: (18, [(2, 68)]),
         1: (16, [(4, 27)]),
         2: (24, [(4, 19)]),
         3: (28, [(4, 15)])},
}

TOTAL_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172}

ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

ECC_BITS = {0: 0b01, 1: 0b00, 2: 0b11, 3: 0b10}   # L / M / Q / H
ECC_NAME = {"L": 0, "M": 1, "Q": 2, "H": 3, 0: 0, 1: 1, 2: 2, 3: 3}

# 字节模式容量（ISO/IEC 18004 标准表，顺序 L/M/Q/H），版本 1~6
PUBLISHED_BYTE_CAPACITY = {
    1: (17, 14, 11, 7),
    2: (32, 26, 20, 14),
    3: (53, 42, 32, 24),
    4: (78, 62, 46, 34),
    5: (106, 84, 60, 44),
    6: (134, 106, 74, 58),
}


def capacity_bytes(version, ecc):
    """该版本在该纠错等级下，字节模式可容纳的最大数据字节数。

    采用 ISO/IEC 18004 公布的标准数值（1-L=17、1-M=14、1-Q=11、1-H=7 …）。
    编码器在极端情况下（数据恰好填满容量、放不下 4 位结束符）会按标准截断，
    因此接受再大 1 字节的数据也不会产生非法码；但对外仍以标准值为准。
    """
    blk = ECC_TABLE[version][ECC_NAME[ecc]]
    data_cw = sum(cnt * dc for cnt, dc in blk[1])
    limit = (data_cw * 8 - 4 - 4) // 8          # 保留 4 位模式 + 4 位结束符
    return min(limit, PUBLISHED_BYTE_CAPACITY[version][ECC_NAME[ecc]])


# ---------------------------------------------------------------- GF(256)
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


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree):
    """生成多项式 (x-a^0)(x-a^1)...(x-a^(degree-1))，系数从高次到低次。"""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c                      # 乘 x
            nxt[j + 1] ^= _gmul(c, _EXP[i])  # 乘 a^i
        poly = nxt
    return poly


def _rs_remainder(data, degree):
    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            lf = _LOG[factor]
            for i in range(degree):
                if gen[i + 1]:
                    rem[i] ^= _EXP[lf + _LOG[gen[i + 1]]]
    return rem


# ---------------------------------------------------------------- 数据编码
def _encode_data(data, version, ecc):
    blk = ECC_TABLE[version][ECC_NAME[ecc]]
    ecc_len, groups = blk
    data_cw = sum(cnt * dc for cnt, dc in groups)

    bits = []

    def put(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                      # 字节模式
    put(len(data), 8 if version <= 9 else 16)
    for b in data:
        put(b, 8)
    cap_bits = data_cw * 8
    # 结束符最多 4 位；剩余空间不足 4 位时，标准要求直接截断到容量（不加结束符）
    if len(bits) + 4 <= cap_bits:
        put(0, 4)
    del bits[cap_bits:]
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < cap_bits:
        put(pad[i % 2], 8)
        i += 1

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                 for i in range(0, len(bits), 8)]

    # 分块 + 纠错
    blocks, eccs, pos = [], [], 0
    for cnt, dc in groups:
        for _ in range(cnt):
            chunk = codewords[pos:pos + dc]
            pos += dc
            blocks.append(chunk)
            eccs.append(_rs_remainder(chunk, ecc_len))

    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ecc_len):
        for e in eccs:
            out.append(e[i])
    return out


# ---------------------------------------------------------------- 矩阵
def _bch_format(fmt):
    """格式信息：5 位数据 + BCH(15,5) 纠错，再异或 0x5412。"""
    v = fmt << 10
    for i in range(4, -1, -1):
        if v & (1 << (i + 10)):
            v ^= 0x537 << i
    return ((fmt << 10) | v) ^ 0x5412


def _version_bits(version):
    v = version << 12
    for i in range(5, -1, -1):
        if v & (1 << (i + 12)):
            v ^= 0x1F25 << i
    return (version << 12) | v


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def make_matrix(data, ecc="M", version=None, mask=None):
    """把 bytes 编码成 QR 矩阵，返回 [[0/1, ...], ...]（1 = 黑）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    ecc_i = ECC_NAME[ecc]
    if version is None:
        for v in sorted(ECC_TABLE):
            if len(data) <= capacity_bytes(v, ecc_i):
                version = v
                break
        if version is None:
            raise ValueError("数据过长（%d 字节），本实现最大支持 6 版 / %d 字节"
                             % (len(data), capacity_bytes(6, ecc_i)))
    if len(data) > capacity_bytes(version, ecc_i):
        raise ValueError("数据 %d 字节超出 %d 版 %s 级容量 %d 字节"
                         % (len(data), version, ecc, capacity_bytes(version, ecc_i)))

    size = version * 4 + 17
    mat = [[0] * size for _ in range(size)]
    res = [[False] * size for _ in range(size)]   # True = 功能图形，不参与掩码

    def set_f(r, c, val):
        mat[r][c] = val
        res[r][c] = True

    # 定位图形 + 分隔符
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    inside = 0 <= dr <= 6 and 0 <= dc <= 6
                    dark = inside and (dr in (0, 6) or dc in (0, 6)
                                       or (2 <= dr <= 4 and 2 <= dc <= 4))
                    set_f(r, c, 1 if dark else 0)

    # 校正图形
    centers = ALIGN[version]
    for r in centers:
        for c in centers:
            if (r < 8 and c < 8) or (r < 8 and c > size - 9) or (r > size - 9 and c < 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    set_f(r + dr, c + dc, 1 if dark else 0)

    # 定时图形
    for i in range(8, size - 8):
        if not res[6][i]:
            set_f(6, i, 1 if i % 2 == 0 else 0)
        if not res[i][6]:
            set_f(i, 6, 1 if i % 2 == 0 else 0)

    # 版本信息（版本 >= 7 才需要，本实现最大 6 版，仅保留逻辑方便扩展）
    if version >= 7:
        vb = _version_bits(version)
        for i in range(18):
            bit = (vb >> i) & 1
            a, b = i // 3, i % 3
            set_f(size - 11 + b, a, bit)
            set_f(a, size - 11 + b, bit)

    # 预留格式信息区 + 固定的黑模块
    for i in range(9):
        if not res[8][i]:
            set_f(8, i, 0)
        if not res[i][8]:
            set_f(i, 8, 0)
    for i in range(8):
        if not res[8][size - 1 - i]:
            set_f(8, size - 1 - i, 0)
        if not res[size - 1 - i][8]:
            set_f(size - 1 - i, 8, 0)
    set_f(size - 8, 8, 1)

    # 数据位蛇形填充
    bits = []
    for cw in _encode_data(data, version, ecc_i):
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx, upward = 0, True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not res[row][c] and idx < len(bits):
                    mat[row][c] = bits[idx]
                    idx += 1
        upward = not upward
        col -= 2

    # 掩码选择
    def apply_mask(m):
        out = [row[:] for row in mat]
        fn = MASKS[m]
        for r in range(size):
            for c in range(size):
                if not res[r][c] and fn(r, c):
                    out[r][c] ^= 1
        return out

    def penalty(g):
        score = 0
        for line in list(g) + [list(col) for col in zip(*g)]:
            run, prev = 1, line[0]
            for v in line[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run, prev = 1, v
            if run >= 5:
                score += 3 + (run - 5)
        for r in range(size - 1):
            for c in range(size - 1):
                if g[r][c] == g[r][c + 1] == g[r + 1][c] == g[r + 1][c + 1]:
                    score += 3
        pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
        for line in list(g) + [list(col) for col in zip(*g)]:
            for i in range(len(line) - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    score += 40
        dark = sum(sum(row) for row in g)
        ratio = dark * 100 // (size * size)
        score += 10 * (abs(ratio - 50) // 5)
        return score

    def with_format(g, m):
        """把格式信息（两份副本）与固定的黑模块写入矩阵副本。"""
        cand = [row[:] for row in g]
        fmt = _bch_format((ECC_BITS[ecc_i] << 3) | m)
        for i in range(15):
            bit = (fmt >> i) & 1
            # 左上
            if i < 6:
                cand[8][i] = bit
            elif i == 6:
                cand[8][7] = bit
            elif i == 7:
                cand[8][8] = bit
            elif i == 8:
                cand[7][8] = bit
            else:
                cand[14 - i][8] = bit
            # 副本：右上 + 左下
            if i < 8:
                cand[8][size - 1 - i] = bit
            else:
                cand[size - 15 + i][8] = bit
        cand[size - 8][8] = 1
        return cand

    if mask is not None:
        return with_format(apply_mask(mask), mask)

    best, best_m = None, None
    for m in range(8):
        cand = with_format(apply_mask(m), m)
        s = penalty(cand)
        if best is None or s < best:
            best, best_m = s, m
    return with_format(apply_mask(best_m), best_m)


# ---------------------------------------------------------------- 终端渲染
def render_lines(matrix, quiet=4, half_block=True):
    """把矩阵渲染成适合终端显示的文本行（前景为黑块，背景留给终端）。"""
    size = len(matrix)
    pad = quiet
    total = size + pad * 2
    grid = [[0] * total for _ in range(total)]
    for r in range(size):
        for c in range(size):
            grid[r + pad][c + pad] = matrix[r][c]

    lines = []
    if half_block:
        # 上半块 + 下半块，一个字符高度表示两行，宽高比接近正方形
        for r in range(0, total, 2):
            top = grid[r]
            bottom = grid[r + 1] if r + 1 < total else [0] * total
            buf = []
            for c in range(total):
                t, b = top[c], bottom[c]
                buf.append("█" if (t and b) else "▀" if t else "▄" if b else " ")
            lines.append("".join(buf))
    else:
        for r in range(total):
            lines.append("".join("██" if v else "  " for v in grid[r]))
    return lines


def qr_lines(text, ecc="M", quiet=4, half_block=True, version=None):
    return render_lines(make_matrix(text, ecc=ecc, version=version),
                        quiet=quiet, half_block=half_block)


if __name__ == "__main__":
    import sys
    payload = sys.argv[1] if len(sys.argv) > 1 else "https://music.163.com/login?codekey=test"
    hb = "--full" not in sys.argv
    for line in qr_lines(payload, half_block=hb):
        print(line)
    print("payload: %s (%d bytes)" % (payload, len(payload.encode("utf-8"))))
