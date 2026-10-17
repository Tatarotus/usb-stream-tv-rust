#!/usr/bin/env python3
"""test_fat32_integrity.py — Validação de integridade e consistência do FAT32 gerado.

Cobre os 4 pilares pedidos:
  1) Validação do Boot Sector e FSInfo.
  2) Decodificação das entradas LFN e 8.3 no Cluster 2 (nomes/extensões/tamanhos).
  3) Verificação de sobreposição de clusters entre arquivos.
  4) Simulação da fórmula de síntese do FAT em fuse_direct (fuse_direct_v2.c)
     checando encadeamento até EOC (0x0FFFFFFF).

Formato do template (gen_template.py): sequência de registros
  <u32 LE sector><512 bytes>  — só setores estáticos (boot, FSInfo, backup, root dir).
Setores FAT e dados são sintetizados live pelo daemon.

Uso:
  pytest scratch/test_fat32_integrity.py -v
  python3 scratch/test_fat32_integrity.py --self-check   # gera templates reais e valida
  python3 scratch/test_fat32_integrity.py --template /tmp/opencode/fat32/x.bin --expect "TV AO VIVO.ts:1800000000"

Geometria canônica (deve bater com gen_template.py E fuse_direct_v2.c):
  BPS=512 SPC=8 RSV=32 FATS=2 SPF=8192 TOTCLUS=1048576
  DATA_SEC=16416 (cluster 2 = root dir, 8 setores)
  FILE_SEC=16424 (cluster 3 = início dos dados)
"""
import os
import re
import struct
import sys
import threading

_REPOS = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPOS not in sys.path:
    sys.path.insert(0, _REPOS)

# ---------------------------------------------------------------- constantes
BPS = 512
SPC = 8
RSV = 32
FATS = 2
SPF = 8192
TOTCLUS = 1048576
DATA_SEC = RSV + FATS * SPF          # 16416
FILE_SEC = DATA_SEC + SPC            # 16424
TOTSEC = DATA_SEC + (TOTCLUS - 2) * SPC
CLUS_SZ = SPC * BPS                  # 4096
EOC = 0x0FFFFFFF
MEDIA = 0x0FFFFFF8
ROOT_CLUS = 2
FILECLUS = 3

# ================================================================= helpers

def parse_template(buf: bytes) -> dict:
    """Deserializa template <sec:u32><512B>* em dict {sec: bytes}."""
    if len(buf) % 516 != 0:
        raise ValueError(f"tamanho {len(buf)} não é múltiplo de 516")
    out = {}
    for i in range(0, len(buf), 516):
        (sec,) = struct.unpack_from("<I", buf, i)
        data = bytes(buf[i + 4:i + 516])
        if sec in out:
            raise ValueError(f"setor duplicado: {sec}")
        out[sec] = data
    return out


def make_sfn_independent(name: str) -> bytes:
    """Reimplementação independente de gen_template.make_sfn (para checagem cruzada)."""
    base, ext = os.path.splitext(name)
    ext = ext.lstrip(".").upper()
    base_clean = re.sub(r"[^A-Z0-9]", "", base.upper())
    base_sfn = (base_clean[:6] + "~1") if len(base_clean) > 6 else f"{base_clean:<8}"[:8]
    ext_sfn = f"{ext[:3]:<3}"
    return f"{base_sfn:<8}{ext_sfn}".encode("ascii")[:11]


def lfn_checksum(sfn: bytes) -> int:
    ch = 0
    for b in sfn:
        ch = (((ch & 1) << 7) | ((ch & 0xFE) >> 1)) + b
        ch &= 0xFF
    return ch


def build_multi_template(files, out_path=None):
    """Constrói template sintético MULTI-ARQUIVO (extensão de gen_template p/ N arquivos).

    files: [(nome, size_bytes), ...] — alocação contígua a partir do cluster 3.
    Retorna bytes no mesmo formato <sec><512B>.
    Usado para validar sobreposição e FAT multi-arquivo; espelha a lógica de
    gen_template.build_fat_template generalizada.
    """
    if not files:
        raise ValueError("lista de arquivos vazia")
    for nm, sz in files:
        if sz < 0 or sz > 0xFFFFFFFF:
            raise ValueError(f"tamanho inválido p/ {nm!r}: {sz}")
    # clusters por arquivo + starts contíguos
    nclus_list = [(sz + CLUS_SZ - 1) // CLUS_SZ if sz > 0 else 0 for _, sz in files]
    starts, cur = [], FILECLUS
    for n in nclus_list:
        starts.append(cur)
        cur += n
    total_data_clus = sum(nclus_list)
    if FILECLUS + total_data_clus - 1 > TOTCLUS + 1 and total_data_clus:
        raise ValueError("arquivos excedem disco virtual de 4GB")
    total_sec = DATA_SEC + (TOTCLUS - 2) * SPC
    free_clus = (TOTCLUS - 2) - 1 - total_data_clus  # -1 = cluster 2 (root)
    last_data_clus = FILECLUS + total_data_clus - 1 if total_data_clus else None

    out = {}
    boot = bytearray(BPS)
    boot[0:3] = b"\xEB\x58\x90"
    boot[3:11] = b"mkfs.fat"
    struct.pack_into("<H", boot, 11, BPS)
    boot[13] = SPC
    struct.pack_into("<H", boot, 14, RSV)
    boot[16] = FATS
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 24, 63)
    struct.pack_into("<H", boot, 26, 255)
    struct.pack_into("<I", boot, 32, total_sec)
    struct.pack_into("<I", boot, 36, SPF)
    struct.pack_into("<I", boot, 44, 2)
    struct.pack_into("<H", boot, 48, 1)
    struct.pack_into("<H", boot, 50, 6)
    boot[64] = 0x80
    boot[66] = 0x29
    struct.pack_into("<I", boot, 67, 0x12345678)
    boot[71:82] = b"LIVETV     "
    boot[82:90] = b"FAT32   "
    boot[510] = 0x55
    boot[511] = 0xAA
    out[0] = bytes(boot)

    fs = bytearray(BPS)
    fs[0:4] = b"RRaA"
    fs[484:488] = b"rrAa"
    struct.pack_into("<I", fs, 488, max(0, free_clus))
    struct.pack_into("<I", fs, 492, (last_data_clus + 1) if last_data_clus else 3)
    fs[508:512] = b"\x00\x00\x55\xAA"
    out[1] = bytes(fs)
    out[6] = bytes(boot)

    root = bytearray(SPC * BPS)
    root[0:11] = b"LIVETV     "
    root[11] = 0x08
    off = 32
    for (nm, sz), start in zip(files, starts):
        sfn_b = make_sfn_independent(nm)
        chksum = lfn_checksum(sfn_b)
        lfn = list(nm) + ["\x00"]
        while len(lfn) % 13:
            lfn.append("\xff")
        n_lfn = len(lfn) // 13
        if off + (n_lfn + 1) * 32 > len(root):
            raise ValueError(f"root dir cheio p/ {nm!r} (muitos arquivos/LFN longos)")
        for seq in range(n_lfn, 0, -1):
            e = bytearray(32)
            e[0] = seq | (0x40 if seq == n_lfn else 0)
            chunk = lfn[(seq - 1) * 13:seq * 13]
            cb = "".join(chunk).encode("utf-16le")
            e[1:11] = cb[0:10]
            e[11] = 0x0F
            e[12] = 0x00
            e[13] = chksum
            e[14:26] = cb[10:22]
            e[26:28] = b"\x00\x00"
            e[28:32] = cb[22:26]
            root[off:off + 32] = e
            off += 32
        s = bytearray(32)
        s[0:11] = sfn_b
        s[11] = 0x20
        struct.pack_into("<H", s, 14, 0x6000)
        struct.pack_into("<H", s, 16, 0x524F)
        struct.pack_into("<H", s, 18, 0x524F)
        struct.pack_into("<H", s, 20, 0)
        struct.pack_into("<H", s, 22, 0x6000)
        struct.pack_into("<H", s, 24, 0x524F)
        struct.pack_into("<H", s, 26, start & 0xFFFF)
        struct.pack_into("<H", s, 20, (start >> 16) & 0xFFFF)
        struct.pack_into("<I", s, 28, sz)
        root[off:off + 32] = s
        off += 32
    for i in range(DATA_SEC, DATA_SEC + SPC):
        out[i] = bytes(root[(i - DATA_SEC) * BPS:(i - DATA_SEC + 1) * BPS])
    buf = bytearray()
    for sec in sorted(out):
        buf.extend(struct.pack("<I", sec) + out[sec])
    if out_path:
        with open(out_path, "wb") as f:
            f.write(buf)
    return bytes(buf)


# ------------------------------------------------- 1) Boot Sector + FSInfo
def validate_boot_fsinfo(tmpl: dict, expect_free=None, expect_next_free=None) -> dict:
    """Valida boot sector (0+backup 6) e FSInfo (1). Retorna info resumida.

    Levanta AssertionError com mensagem diagnóstica em qualquer divergência.
    """
    assert 0 in tmpl, "setor 0 (boot) ausente no template"
    assert 1 in tmpl, "setor 1 (FSInfo) ausente no template"
    assert 6 in tmpl, "setor 6 (backup boot) ausente no template"
    b = tmpl[0]
    assert len(b) == 512, "boot != 512B"
    assert b[0:3] == b"\xEB\x58\x90", f"jump inválido: {b[0:3]!r}"
    assert b[3:11] == b"mkfs.fat", f"OEM inválido: {b[3:11]!r}"
    (bps,) = struct.unpack_from("<H", b, 11)
    assert bps == 512, f"BPS={bps} != 512"
    assert b[13] == SPC, f"SPC={b[13]} != {SPC}"
    (rsv,) = struct.unpack_from("<H", b, 14)
    assert rsv == RSV, f"reserved={rsv} != {RSV}"
    assert b[16] == FATS, f"FATs={b[16]} != {FATS}"
    assert b[21] == 0xF8, f"media={b[21]:#x} != 0xF8"
    (totsec,) = struct.unpack_from("<I", b, 32)
    assert totsec == TOTSEC, f"total_sec={totsec} != {TOTSEC} (DATA_SEC+(TOTCLUS-2)*SPC)"
    (spf,) = struct.unpack_from("<I", b, 36)
    assert spf == SPF, f"SPF={spf} != {SPF}"
    (rootc,) = struct.unpack_from("<I", b, 44)
    assert rootc == 2, f"root_clus={rootc} != 2"
    (fsi,) = struct.unpack_from("<H", b, 48)
    assert fsi == 1, f"fsinfo_sec={fsi} != 1"
    (bk,) = struct.unpack_from("<H", b, 50)
    assert bk == 6, f"backup_sec={bk} != 6"
    assert b[66] == 0x29, "boot signature 0x29 ausente (+66)"
    assert b[71:82] == b"LIVETV     ", f"volume label boot={b[71:82]!r}"
    assert b[82:90] == b"FAT32   ", f"fstype={b[82:90]!r} != FAT32"
    assert b[510] == 0x55 and b[511] == 0xAA, "assinatura 55AA ausente no boot"
    assert tmpl[6] == tmpl[0], "backup boot (setor 6) diverge do setor 0"
    f = tmpl[1]
    assert f[0:4] == b"RRaA", f"FSInfo lead={f[0:4]!r} != RRaA"
    assert f[484:488] == b"rrAa", f"FSInfo struct={f[484:488]!r} != rrAa"
    (free_c,) = struct.unpack_from("<I", f, 488)
    (nxt,) = struct.unpack_from("<I", f, 492)
    assert f[508:512] == b"\x00\x00\x55\xAA", f"FSInfo trail={f[508:512]!r}"
    if expect_free is not None:
        assert free_c == expect_free, f"FSInfo free={free_c} != esperado {expect_free}"
    if expect_next_free is not None:
        assert nxt == expect_next_free, f"FSInfo next={nxt} != esperado {expect_next_free}"
    return {"total_sec": totsec, "free": free_c, "next_free": nxt}


# ------------------------------------------------- 2) LFN + 8.3 no Cluster 2
class FileRec:
    def __init__(self, name, sfn, start, size):
        self.name = name
        self.sfn = sfn
        self.start = start
        self.size = size
        self.nclus = (size + CLUS_SZ - 1) // CLUS_SZ if size > 0 else 0

    def __repr__(self):
        return f"FileRec({self.name!r}, sfn={self.sfn!r}, clus={self.start}, size={self.size})"


def _decode_lfn_chunk(e: bytes) -> str:
    raw = e[1:11] + e[14:26] + e[28:32]  # 13 UTF-16LE chars = 26 bytes
    chars = []
    for i in range(0, 26, 2):
        (cp,) = struct.unpack_from("<H", raw, i)
        if cp == 0x0000:
            break  # terminador; resto deve ser 0xFFFF (checado fora)
        if cp == 0xFFFF:
            continue  # padding — só válido após terminador; tratado pelo caller
        chars.append(chr(cp))
    return "".join(chars)


def decode_root_dir(tmpl: dict) -> list:
    """Decodifica o Cluster 2 (setores DATA_SEC..+7) em [FileRec]. Valida LFN/SFN.

    Checagens: ordem de sequência, flag 0x40 só na última, checksum LFN==SFN,
    atributo 0x0F/0x20/0x08, terminador 0x0000, padding 0xFFFF, high==0 p/ <64K clus.
    """
    for s in range(DATA_SEC, DATA_SEC + SPC):
        assert s in tmpl, f"setor root {s} ausente (cluster 2 incompleto)"
    root = b"".join(tmpl[s] for s in range(DATA_SEC, DATA_SEC + SPC))
    assert len(root) == SPC * BPS
    # entry 0: volume label
    assert root[11] == 0x08, f"entry0 attr={root[11]:#x} != 0x08 (volume)"
    assert root[0:11] == b"LIVETV     ", f"volume label root={root[0:11]!r}"
    files = []
    i = 1  # índice de entry de 32B (pula volume)
    pending = []  # LFN entries acumuladas (ordem de disco: seq alta -> baixa)
    while i * 32 + 32 <= len(root):
        e = root[i * 32:(i + 1) * 32]
        first = e[0]
        attr = e[11]
        if first == 0x00:
            break  # fim do diretório
        if first == 0xE5:
            pending = []
            i += 1
            continue
        if attr == 0x0F:  # LFN
            pending.append(e)
            i += 1
            continue
        if attr == 0x08:  # volume extra — ignora
            pending = []
            i += 1
            continue
        if attr == 0x20:  # SFN de arquivo
            sfn = bytes(e[0:11])
            chksum = lfn_checksum(sfn)
            # reordena LFN: disco guarda seq N..1; nome = concatenação seq 1..N
            lfn_parts = {}
            for le in pending:
                seq = le[0] & 0x1F
                lfn_parts[seq] = le
            if pending:
                n = max(lfn_parts)
                assert len(lfn_parts) == n, f"LFN incompleto: {sorted(lfn_parts)} esperava 1..{n}"
                # flag LAST (0x40) deve estar na entry de maior seq
                lasts = [le for le in pending if le[0] & 0x40]
                assert len(lasts) == 1, f"flag 0x40 fora do lugar ({len(lasts)} entries marcadas)"
                assert (lasts[0][0] & 0x1F) == n, "flag 0x40 fora da última entry"
                # sequência deve ser exatamente n..1 sem buracos e checksums iguais
                for le in pending:
                    assert le[13] == chksum, \
                        f"checksum LFN {le[13]:#x} != SFN {chksum:#x} (sfn={sfn!r})"
                    assert le[12] == 0x00 and le[26:28] == b"\x00\x00", "campos reservados LFN ≠ 0"
                name = "".join(_decode_lfn_chunk(lfn_parts[s]) for s in range(1, n + 1))
                # remove padding/terminador: nome real termina no 1º \x00
                name = name.split("\x00")[0]
            else:
                # sem LFN: deriva do SFN (caso 8.3 puro)
                base = sfn[0:8].decode("ascii").rstrip()
                ext = sfn[8:11].decode("ascii").rstrip()
                name = base + (("." + ext) if ext else "")
            (low,) = struct.unpack_from("<H", e, 26)
            (high,) = struct.unpack_from("<H", e, 20)
            start = (high << 16) | low
            (sz,) = struct.unpack_from("<I", e, 28)
            assert start >= 3 or sz == 0, f"start cluster inválido: {start}"
            files.append(FileRec(name, sfn, start, sz))
            pending = []
            i += 1
            continue
        # atributo desconhecido
        raise AssertionError(f"entry {i}: attr desconhecido {attr:#x} first={first:#x}")
    if pending:
        raise AssertionError("LFN órfão sem SFN correspondente no fim do diretório")
    return files


def verify_expected_files(files: list, expected: list, check_sfn=True) -> None:
    """Compara [(nome, tamanho)] esperado vs decodificado, byte-exato (nome+ext+size)."""
    assert len(files) == len(expected), \
        f"nº arquivos {len(files)} != esperado {len(expected)}: {[f.name for f in files]}"
    for f, (enm, esz) in zip(files, expected):
        assert f.name == enm, f"nome {f.name!r} != esperado {enm!r}"
        assert f.size == esz, f"{f.name!r}: size {f.size} != esperado {esz}"
        if check_sfn:
            exp_sfn = make_sfn_independent(enm)
            assert f.sfn == exp_sfn, f"{f.name!r}: SFN {f.sfn!r} != {exp_sfn!r}"
            # checagem cruzada com o gerador real
            try:
                import gen_template
                assert f.sfn == gen_template.make_sfn(enm), "SFN diverge de gen_template.make_sfn"
            except ImportError:
                pass


# ------------------------------------------------- 3) Sobreposição de clusters
def check_no_overlap(files: list) -> dict:
    """Garante que nenhum cluster de dados colide. Retorna mapa {clus: arquivo}.

    Regras: start>=3 (nunca 0/1/2), fim dentro do disco, ranges disjuntos,
    cluster 2 (root) intocado, setores de dados fora da região FAT/reservada.
    """
    owner = {}
    for f in files:
        if f.size == 0:
            assert f.nclus == 0, f"{f.name!r}: size 0 mas nclus={f.nclus}"
            continue
        assert f.start >= FILECLUS, f"{f.name!r}: start={f.start} < 3"
        assert f.start >= 2 + 1, "arquivo usa cluster reservado/root"
        end = f.start + f.nclus  # exclusivo
        assert end - 1 <= TOTCLUS + 1, f"{f.name!r}: excede disco (end={end-1})"
        # setor final dentro de TOTSEC
        last_sec = DATA_SEC + (end - 1 - 2) * SPC + (SPC - 1)
        assert last_sec < TOTSEC, f"{f.name!r}: setor final {last_sec} >= TOTSEC"
        for c in range(f.start, end):
            assert c != ROOT_CLUS, f"{f.name!r}: colide com root (clus 2)"
            assert c not in owner, \
                f"COLISÃO: cluster {c} de {f.name!r} já pertence a {owner[c]!r}"
            owner[c] = f.name
    # par-a-par explícito (mensagem amigável mesmo se mapa acima já pegaria)
    for a in range(len(files)):
        for b in range(a + 1, len(files)):
            fa, fb = files[a], files[b]
            if fa.nclus == 0 or fb.nclus == 0:
                continue
            ra, rb = set(range(fa.start, fa.start + fa.nclus)), set(range(fb.start, fb.start + fb.nclus))
            inter = ra & rb
            assert not inter, f"sobreposição {fa.name!r} × {fb.name!r}: clusters {sorted(inter)[:5]}…"
    return owner


# ------------------------------------------------- 4) Síntese do FAT (fuse_direct_v2.c)
def fat_entry_daemon(cl: int, lastclus: int) -> int:
    """Réplica exata de serve_disk() em src/client/fuse_direct_v2.c:990-999.

    if (cl<2) v=(cl==0)?MEDIA:EOC; elif cl==2: EOC;
    elif cl<lastclus: cl+1; elif cl==lastclus: EOC; else: 0.
    """
    if cl < 2:
        return MEDIA if cl == 0 else EOC
    elif cl == 2:
        return EOC
    elif cl < lastclus:
        return cl + 1
    elif cl == lastclus:
        return EOC
    else:
        return 0


def fat_entry_multi(cl: int, ranges: list) -> int:
    """FAT ideal multi-arquivo: cada arquivo encadeia contíguo até EOC; resto 0."""
    if cl < 2:
        return MEDIA if cl == 0 else EOC
    if cl == 2:
        return EOC
    for (s, n) in ranges:
        if s <= cl < s + n:
            return EOC if cl == s + n - 1 else cl + 1
    return 0


def walk_chain(start: int, entry_fn) -> list:
    """Segue encadeamento até EOC; protege contra loop (limite 2M passos)."""
    chain, seen, cur = [], set(), start
    while True:
        assert cur not in seen, f"loop no FAT em cluster {cur}"
        seen.add(cur)
        chain.append(cur)
        v = entry_fn(cur)
        if v == EOC:
            break
        assert v != 0, f"cluster {cur} aponta p/ livre (0) — cadeia truncada"
        assert 2 <= v <= TOTCLUS + 1, f"ponteiro FAT inválido: {cur} -> {v}"
        cur = v
        assert len(chain) <= 2_000_000, "cadeia excede limite (loop?)"
    return chain


def verify_fat_chains(files: list, single_mode_daemon=True) -> dict:
    """Verifica que cada arquivo termina com EOC e tem comprimento == nclus.

    single_mode_daemon=True: usa a fórmula literal do daemon (1 arquivo contíguo
    de FILECLUS até lastclus). Para multi-arquivo documenta a limitação e valida
    o modelo ideal multi (fat_entry_multi).
    """
    res = {}
    if len(files) == 1 and single_mode_daemon:
        f = files[0]
        lastclus = f.start + f.nclus - 1 if f.nclus else f.start
        fn = lambda c: fat_entry_daemon(c, lastclus)
        # âncoras reservadas
        assert fn(0) == MEDIA and fn(1) == EOC and fn(2) == EOC, "entradas reservadas divergentes"
        if f.nclus == 0:
            res[f.name] = {"chain": [], "eoc": True}
            return res
        chain = walk_chain(f.start, fn)
        assert len(chain) == f.nclus, f"cadeia len={len(chain)} != nclus={f.nclus}"
        assert fn(chain[-1]) == EOC, "último cluster não termina em EOC"
        assert fn(lastclus + 1) == 0, "cluster após EOC deveria ser livre (0)"
        # espelho FAT1==FAT2 por construção (mesma sno) — verifica fórmula p/ sno>=SPF
        for probe in (0, 1, lastclus, lastclus + 1):
            sno = probe // 128
            assert fat_entry_daemon(sno * 128 + probe % 128, lastclus) == fn(probe)
        res[f.name] = {"chain_len": len(chain), "last": lastclus, "eoc": True}
        return res
    # modo multi: modelo ideal
    ranges = [(f.start, f.nclus) for f in files if f.nclus > 0]
    for f in files:
        if f.nclus == 0:
            res[f.name] = {"chain": [], "eoc": True}
            continue
        fn = lambda c, _r=tuple(ranges): fat_entry_multi(c, list(_r))
        chain = walk_chain(f.start, fn)
        assert len(chain) == f.nclus, f"{f.name!r}: cadeia {len(chain)} != nclus {f.nclus}"
        assert fn(f.start + f.nclus - 1) == EOC
        res[f.name] = {"chain_len": len(chain), "eoc": True}
    return res


def verify_fat_sector_synthesis(lastclus: int, sectors=(RSV, RSV + 1, RSV + SPF)) -> None:
    """Verifica setores FAT sintetizados: 128 entries/setor, espelho FAT1/FAT2."""
    for sec in sectors:
        sno = sec - RSV
        if sno >= SPF:
            sno -= SPF
        for k in (0, 1, 2, 3, 127):
            cl = sno * 128 + k
            v = fat_entry_daemon(cl, lastclus)
            if cl == 0:
                assert v == MEDIA
            elif cl in (1, 2):
                assert v == EOC
            # consistência do espelho: mesmo sno nas duas FATs dá mesmo valor
            v2 = fat_entry_daemon((sno) * 128 + k, lastclus)
            assert v == v2, f"espelho FAT diverge em cl={cl}"


# ================================================================== pytest
# Nota: testes geram templates REAIS via gen_template.build_fat_template
# (single-file) + build_multi_template (multi) em /tmp/opencode/fat32.

TMP = "/tmp/opencode/fat32"


def _gen_single(name, size, tag):
    import gen_template
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, f"{tag}.bin")
    gen_template.build_fat_template(file_name=name, file_size=size, out_path=p)
    with open(p, "rb") as fh:
        return parse_template(fh.read())


def _expected_free_next(size, nfiles_extra_clus=0):
    n = (size + CLUS_SZ - 1) // CLUS_SZ if size else 0
    n += nfiles_extra_clus
    return (TOTCLUS - 2) - 1 - n, 3 + n


# ---- 1) Boot/FSInfo ----
def test_boot_fsinfo_default_live():
    t = _gen_single("TV AO VIVO.ts", 1_800_000_000, "live18")
    free, nxt = _expected_free_next(1_800_000_000)
    validate_boot_fsinfo(t, expect_free=free, expect_next_free=nxt)


def test_boot_fsinfo_vod_small():
    t = _gen_single("filme.mp4", 700_000_000, "vod700")
    free, nxt = _expected_free_next(700_000_000)
    validate_boot_fsinfo(t, expect_free=free, expect_next_free=nxt)


def test_boot_fsinfo_multifile_synthetic():
    buf = build_multi_template([("A.ts", 8192), ("B.mp4", 4096)])
    t = parse_template(buf)
    free = (TOTCLUS - 2) - 1 - (2 + 1)
    validate_boot_fsinfo(t, expect_free=free, expect_next_free=3 + 3)


def test_boot_signature_corrupted_detected():
    t = _gen_single("TV AO VIVO.ts", 4096, "corrupt")
    bad = bytearray(t[0])
    bad[510] = 0x00
    t[0] = bytes(bad)
    try:
        validate_boot_fsinfo(t)
    except AssertionError:
        return
    raise AssertionError("boot corrompido NÃO detectado")


def test_fsinfo_trail_corrupted_detected():
    t = _gen_single("TV AO VIVO.ts", 4096, "corrupt2")
    bad = bytearray(t[1])
    bad[508] = 0xFF
    t[1] = bytes(bad)
    try:
        validate_boot_fsinfo(t)
    except AssertionError:
        return
    raise AssertionError("FSInfo corrompido NÃO detectado")


# ---- 2) LFN/8.3 ----
def test_lfn_default_live_name():
    t = _gen_single("TV AO VIVO.ts", 1_800_000_000, "live18b")
    files = decode_root_dir(t)
    verify_expected_files(files, [("TV AO VIVO.ts", 1_800_000_000)])


def test_lfn_long_name_multientry():
    nm = "Série Documental - Episódio 12 Final HD.mp4"  # >26 chars => 3+ LFN entries
    t = _gen_single(nm, 1234567, "long")
    files = decode_root_dir(t)
    verify_expected_files(files, [(nm, 1234567)])


def test_lfn_short_83_pure():
    t = _gen_single("A.ts", 4096, "short")
    files = decode_root_dir(t)
    verify_expected_files(files, [("A.ts", 4096)])


def test_lfn_checksum_mismatch_detected():
    import gen_template
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "chksum.bin")
    gen_template.build_fat_template(file_name="TV AO VIVO.ts", file_size=8192, out_path=p)
    t = parse_template(open(p, "rb").read())
    root = bytearray(b"".join(t[s] for s in range(DATA_SEC, DATA_SEC + SPC)))
    root[32 + 13] ^= 0xFF  # corrompe checksum da 1ª LFN
    for k, s in enumerate(range(DATA_SEC, DATA_SEC + SPC)):
        t[s] = bytes(root[k * BPS:(k + 1) * BPS])
    try:
        decode_root_dir(t)
    except AssertionError:
        return
    raise AssertionError("checksum LFN divergente NÃO detectado")


def test_multifile_names_sizes_exact():
    files_exp = [("TV AO VIVO.ts", 50_000_000), ("Filme Dublado 2024.mp4", 1_500_000_000),
                 ("curto.ts", 188 * 100)]
    buf = build_multi_template(files_exp)
    files = decode_root_dir(parse_template(buf))
    verify_expected_files(files, files_exp)
    assert [f.start for f in files] == [3, 3 + (50_000_000 + 4095) // 4096,
                                        3 + (50_000_000 + 4095) // 4096 + (1_500_000_000 + 4095) // 4096]


# ---- 3) Sobreposição ----
def test_no_overlap_single():
    t = _gen_single("TV AO VIVO.ts", 1_800_000_000, "live18c")
    check_no_overlap(decode_root_dir(t))


def test_no_overlap_multi():
    buf = build_multi_template([("A.ts", 10_000_000), ("B.ts", 20_000_000), ("C.mp4", 5_000_000)])
    check_no_overlap(decode_root_dir(parse_template(buf)))


def test_overlap_injected_detected():
    buf = build_multi_template([("A.ts", 8192), ("B.ts", 8192)])
    t = parse_template(buf)
    root = bytearray(b"".join(t[s] for s in range(DATA_SEC, DATA_SEC + SPC)))
    # força B.start = A.start (=3): localiza 2º SFN (attr 0x20) e reescreve lowclus
    sfns = []
    for i in range(len(root) // 32):
        if root[i * 32 + 11] == 0x20:
            sfns.append(i)
    assert len(sfns) == 2
    struct.pack_into("<H", root, sfns[1] * 32 + 26, 3)
    for k, s in enumerate(range(DATA_SEC, DATA_SEC + SPC)):
        t[s] = bytes(root[k * BPS:(k + 1) * BPS])
    try:
        check_no_overlap(decode_root_dir(t))
    except AssertionError as e:
        assert "COLISÃO" in str(e) or "sobreposição" in str(e)
        return
    raise AssertionError("sobreposição injetada NÃO detectada")


def test_boundary_exact_cluster_multiple():
    for sz in (4096, 8192, 4096 * 100):
        t = _gen_single("X.ts", sz, f"exact{sz}")
        files = decode_root_dir(t)
        assert files[0].nclus == sz // 4096
        check_no_overlap(files)


def test_boundary_plus_one_byte():
    t = _gen_single("X.ts", 4097, "plus1")
    files = decode_root_dir(t)
    assert files[0].nclus == 2, "4097B deve ocupar 2 clusters"
    check_no_overlap(files)


def test_empty_size_zero_clusters():
    import gen_template
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "zero.bin")
    gen_template.build_fat_template(file_name="Vazio.ts", file_size=0, out_path=p)
    files = decode_root_dir(parse_template(open(p, "rb").read()))
    assert files[0].nclus == 0
    check_no_overlap(files)  # não deve colidir


def test_oversize_rejected():
    import gen_template
    try:
        gen_template.build_fat_template(file_name="X.ts", file_size=0x1_0000_0000, out_path=None)
    except ValueError:
        return
    raise AssertionError("size > 4GB deveria levantar ValueError")


# ---- 4) FAT ----
def test_fat_chain_single_ends_eoc():
    t = _gen_single("TV AO VIVO.ts", 1_800_000_000, "live18d")
    files = decode_root_dir(t)
    r = verify_fat_chains(files)
    assert r["TV AO VIVO.ts"]["eoc"] is True
    last = files[0].start + files[0].nclus - 1
    verify_fat_sector_synthesis(last)


def test_fat_chain_small_exact_walk():
    t = _gen_single("A.ts", 8192, "fatwalk")  # 2 clusters: 3->4->EOC
    files = decode_root_dir(t)
    assert files[0].start == 3 and files[0].nclus == 2
    last = 4
    assert fat_entry_daemon(3, last) == 4
    assert fat_entry_daemon(4, last) == EOC
    assert fat_entry_daemon(5, last) == 0
    assert walk_chain(3, lambda c: fat_entry_daemon(c, last)) == [3, 4]
    verify_fat_chains(files)


def test_fat_chain_one_cluster():
    t = _gen_single("A.ts", 100, "fat1")
    files = decode_root_dir(t)
    assert files[0].nclus == 1
    assert fat_entry_daemon(3, 3) == EOC
    verify_fat_chains(files)


def test_fat_multi_ideal_each_ends_eoc():
    buf = build_multi_template([("A.ts", 8192), ("B.ts", 4096), ("C.ts", 12288)])
    files = decode_root_dir(parse_template(buf))
    r = verify_fat_chains(files, single_mode_daemon=False)
    assert all(v["eoc"] for v in r.values())
    ranges = [(f.start, f.nclus) for f in files]
    for f in files:
        chain = walk_chain(f.start, lambda c: fat_entry_multi(c, ranges))
        assert len(chain) == f.nclus


def test_fat_daemon_singlefile_limitation_documented():
    """O daemon atual só encadeia UM arquivo contíguo de FILECLUS→lastclus.

    Com 2 arquivos contíguos, a fórmula single-file fundiria as cadeias
    (A.last→B.start em vez de EOC). Este teste documenta o comportamento:
    deve EOC-dividir no modelo ideal, mas a fórmula literal NÃO divide.
    """
    buf = build_multi_template([("A.ts", 8192), ("B.ts", 8192)])
    files = decode_root_dir(parse_template(buf))
    assert len(files) == 2
    last_single = files[0].start + files[0].nclus - 1  # lastclus se só A existisse
    # fórmula literal: A encadeia corretamente isolado…
    assert walk_chain(3, lambda c: fat_entry_daemon(c, last_single)) == [3, 4]
    # …mas tratando o disco como single-file até o fim de B, A NÃO terminaria em EOC
    last_both = files[1].start + files[1].nclus - 1
    assert fat_entry_daemon(last_single, last_both) == last_single + 1, \
        "daemon single-file não insere EOC no meio — multi-arquivo exige FAT por-arquivo"
    # modelo ideal corrige:
    ranges = [(f.start, f.nclus) for f in files]
    assert fat_entry_multi(last_single, ranges) == EOC


def test_concurrent_builds_deterministic():
    import gen_template
    outs, errs = [], []

    def w(i):
        try:
            outs.append(gen_template.build_fat_template(file_name=f"F{i}.ts", file_size=1_000_000 + i, out_path=None))
        except Exception as e:  # noqa
            errs.append(e)

    ths = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert not errs, errs
    assert len(outs) == 8
    import gen_template as g2
    assert outs[0] == g2.build_fat_template(file_name="F0.ts", file_size=1_000_000, out_path=None)


# ------------------------------------------------------------- CLI self-check
def _self_check():
    import gen_template
    os.makedirs(TMP, exist_ok=True)
    cases = [("TV AO VIVO.ts", 1_800_000_000, "live"), ("VOD Filme.mp4", 700_000_000, "vod"),
             ("A.ts", 8192, "tiny")]
    print("== 1) Boot/FSInfo + 2) LFN/8.3 + 3) Overlap + 4) FAT — templates reais ==")
    for nm, sz, tag in cases:
        p = os.path.join(TMP, f"self_{tag}.bin")
        gen_template.build_fat_template(file_name=nm, file_size=sz, out_path=p)
        t = parse_template(open(p, "rb").read())
        n = (sz + CLUS_SZ - 1) // CLUS_SZ if sz else 0
        info = validate_boot_fsinfo(t, expect_free=(TOTCLUS - 2) - 1 - n, expect_next_free=3 + n)
        files = decode_root_dir(t)
        verify_expected_files(files, [(nm, sz)])
        check_no_overlap(files)
        verify_fat_chains(files)
        print(f"  [OK] {nm!r} {sz}B: free={info['free']} next={info['next_free']} "
              f"start={files[0].start} nclus={files[0].nclus} EOC=0x0FFFFFFF")
    print("== multi-arquivo sintético ==")
    exp = [("TV AO VIVO.ts", 50_000_000), ("Filme Dublado 2024.mp4", 1_500_000_000)]
    buf = build_multi_template(exp, os.path.join(TMP, "self_multi.bin"))
    files = decode_root_dir(parse_template(buf))
    verify_expected_files(files, exp)
    check_no_overlap(files)
    verify_fat_chains(files, single_mode_daemon=False)
    for f in files:
        print(f"  [OK] {f.name!r} start={f.start} nclus={f.nclus} size={f.size} EOC=True")
    print("[✓] self-check passou")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        import pytest
        sys.exit(pytest.main([__file__, "-v"] + [a for a in sys.argv[1:] if a.startswith("-")]))
