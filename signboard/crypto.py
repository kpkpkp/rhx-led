"""RHX8-Q1C authentication cipher implementation.

The RHX8-Q1C controller uses an RC5-32/12/16 block cipher (32-bit words, 12 rounds,
16-byte key) with a pre-expanded 26-word subkey table S to authenticate write/upload
sessions over BLE.
"""
import struct

# Pre-expanded 26-word subkey table S (RC5-32/12/16) from libLED_CP.so .rodata
SUBKEYS = [
    0x8c546ec0, 0xefc58278, 0xf75565d0, 0x344f5f35, 0xe89b90ae, 0xc0b223a2,
    0x974c46fb, 0x485f5f8c, 0xf3190670, 0xf25eee6c, 0x1f3266c1, 0xfde17066,
    0x607ed91d, 0xfa70780a, 0xc2d2d368, 0x50ac4c1e, 0x0837c948, 0x4a48351c,
    0xf96e2cb7, 0xb558e569, 0x476ec9b0, 0x0029931a, 0x4a7a3661, 0x4fa86f35,
    0x056de029, 0x4ff13261
]

DEFAULT_PASSWORD = None   # no built-in password: direct.py takes it from signboard.config ("sign_password")
SECONDARY_PASSWORD = "12348765"   # fixed by the protocol (same for every controller), not a user secret  # scan:ok


def rotl(val: int, shift: int) -> int:
    shift &= 31
    return (((val << shift) & 0xffffffff) | (val >> (32 - shift))) & 0xffffffff


def rotr(val: int, shift: int) -> int:
    shift &= 31
    return ((val >> shift) | ((val << (32 - shift)) & 0xffffffff)) & 0xffffffff


def encipher_block(A: int, B: int) -> tuple[int, int]:
    """RC5-32/12/16 encryption of a 64-bit block (two 32-bit words)."""
    A = (A + SUBKEYS[0]) & 0xffffffff
    B = (B + SUBKEYS[1]) & 0xffffffff
    for i in range(1, 13):
        A = (rotl(A ^ B, B) + SUBKEYS[2 * i]) & 0xffffffff
        B = (rotl(B ^ A, A) + SUBKEYS[2 * i + 1]) & 0xffffffff
    return A, B


def decipher_block(A: int, B: int) -> tuple[int, int]:
    """RC5-32/12/16 decryption of a 64-bit block (two 32-bit words)."""
    for i in range(12, 0, -1):
        B = rotr((B - SUBKEYS[2 * i + 1]) & 0xffffffff, A) ^ A
        A = rotr((A - SUBKEYS[2 * i]) & 0xffffffff, B) ^ B
    B = (B - SUBKEYS[1]) & 0xffffffff
    A = (A - SUBKEYS[0]) & 0xffffffff
    return A, B


def decrypt(data: bytes) -> bytes:
    """Decrypt 8-byte aligned data using RC5."""
    if len(data) % 8 != 0:
        raise ValueError(f"Data length must be multiple of 8, got {len(data)}")
    out = bytearray()
    for off in range(0, len(data), 8):
        w0, w1 = struct.unpack_from("<II", data, off)
        p0, p1 = decipher_block(w0, w1)
        out += struct.pack("<II", p0, p1)
    return bytes(out)


def encrypt(data: bytes) -> bytes:
    """Encrypt 8-byte aligned data using RC5."""
    if len(data) % 8 != 0:
        raise ValueError(f"Data length must be multiple of 8, got {len(data)}")
    out = bytearray()
    for off in range(0, len(data), 8):
        w0, w1 = struct.unpack_from("<II", data, off)
        c0, c1 = encipher_block(w0, w1)
        out += struct.pack("<II", c0, c1)
    return bytes(out)


def solve_challenge(challenge_bytes: bytes, step: int, password: str = DEFAULT_PASSWORD) -> bytes:
    """
    Solves an 8-byte RHX8-Q1C challenge and returns the 16-byte response.
    
    step: 1 (primary challenge, 59a0 -> responds with primary password)
          2 (secondary challenge, 59a3 -> responds with secondary password)
    """
    if len(challenge_bytes) != 8:
        raise ValueError(f"Challenge must be 8 bytes, got {len(challenge_bytes)}")

    # 1. Decrypt the 8-byte challenge block
    dec = decrypt(challenge_bytes)
    prefix = dec[:4]  # ca 01 <nonce:2> or ca 02 <nonce:2>

    # 2. Select password for step
    pwd = password if step == 1 else SECONDARY_PASSWORD
    pwd_bytes = pwd.encode("utf-8")[:8]

    # 3. Form 16-byte plaintext response: prefix (4B) + len (1B) + pwd (up to 8B) + padding
    plain_resp = bytearray(16)
    plain_resp[0:4] = prefix
    plain_resp[4] = len(pwd_bytes)
    plain_resp[5:5 + len(pwd_bytes)] = pwd_bytes
    # Remainder stays 0x00

    # 4. Encrypt the 16-byte response buffer
    return encrypt(bytes(plain_resp))
