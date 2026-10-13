"""python -m signboard.cli --ble ADDR [on|off|show]"""
import argparse
import json

from signboard import protocol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ble', required=True)
    ap.add_argument('cmd', choices=['on', 'off', 'show'])
    a = ap.parse_args()
    from signboard.ble import BleakTransport
    tr = BleakTransport(a.ble, timeout=10)
    if a.cmd == 'show':
        print(json.dumps(protocol.read_program(tr.request)['project'], ensure_ascii=False)[:400])
    else:
        reply = tr.send(protocol.power(a.cmd == 'on'), 1)
        print('power', a.cmd, '-> reply', reply.hex())


if __name__ == '__main__':
    main()
