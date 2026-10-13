"""Frames, crypto and the program-image container: pure functions, no hardware."""
import struct

import pytest

from conftest import synthetic_image
from signboard import crypto, image, protocol


def test_frames():
    assert protocol.power(True).hex() == 'aa55aa5500000804'
    assert protocol.power(False).hex() == 'aa55aa5500000904'
    assert protocol.open_session().hex() == 'aa55aa5500000e0104'
    assert protocol.close_session().hex() == 'aa55aa5500000e0204'
    assert protocol.finish_upload().hex() == 'aa55aa550000dc04000004'
    assert protocol.upload_request(0).hex() == 'aa55aa55000078e1000000000000000004'
    assert protocol.read_request(0x21, 4).hex() == 'aa55aa550000ac01000000210000000404'


def test_read_reply_roundtrip():
    data = bytes(range(40))
    reply = protocol.DATA_MAGIC + struct.pack('>II', 0x100, 40) + data + struct.pack('>I', sum(data))
    assert protocol.parse_read_reply(reply, 0x100, 40) == data
    with pytest.raises(ValueError):
        protocol.parse_read_reply(reply[:-1] + b'\x00', 0x100, 40)
    with pytest.raises(ValueError):
        protocol.parse_read_reply(reply, 0x104, 40)


def test_cipher_roundtrip():
    block = bytes(range(16))
    assert crypto.decrypt(crypto.encrypt(block)) == block
    assert crypto.encrypt(block) != block


def test_challenge_answer_carries_prefix_and_password():
    chal = crypto.encrypt(b'\xca\x01\x12\x34' + b'\x00' * 4)
    plain = crypto.decrypt(crypto.solve_challenge(chal, 1, 'abc12345'))
    assert plain[:4] == b'\xca\x01\x12\x34' and plain[4] == 8 and plain[5:13] == b'abc12345'
    plain2 = crypto.decrypt(crypto.solve_challenge(chal, 2, 'ignored'))
    assert plain2[5:5 + plain2[4]] == crypto.SECONDARY_PASSWORD.encode()


def test_no_default_password_shipped():
    assert crypto.DEFAULT_PASSWORD is None


def test_image_parse_join_roundtrip():
    img = synthetic_image()
    p = image.parse(img)
    assert p['len_ok'] and p['trailer_ok'] and p['kind'] == 0x75 and p['json'] == b'{"test": true}'
    assert image.join(p) == img


def test_wire_stream_checksums():
    img = synthetic_image()
    packets, size = image.wire_stream(img)
    stream = b''.join(packets)
    body = img[:-6]
    assert all(len(p) <= 500 for p in packets)
    assert stream[:12].hex() == 'aa55aa550000dc0200000000' and struct.unpack_from('>I', stream, 12)[0] == size
    assert size == len(body) - 4 + 6
    s1 = struct.unpack_from('>I', stream, 16 + len(body))[0]
    s2 = struct.unpack_from('>I', stream, 20 + len(body))[0]
    assert s1 == sum(body) & 0xffffffff
    assert s2 == (s1 + sum(struct.pack('>I', s1))) & 0xffffffff
    assert stream.endswith(b'\x00\x00\x04')


def test_wire_stream_refuses_bad_images():
    img = synthetic_image()
    with pytest.raises(ValueError):
        image.wire_stream(b'\x01' + img[1:])                 # no 00 00 00 ff prefix
    with pytest.raises(ValueError):
        image.wire_stream(img[:-6])                           # trailer missing: length field no longer matches
