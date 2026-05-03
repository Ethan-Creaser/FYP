"""
Single-node UWB role cycling diagnostic.

Run on ONE node. No second node needed.

Tests whether configure_warm correctly switches between anchor and tag
by reading AT+GETCFG after each switch and reporting timing.

If GETCFG confirms role changes but UWB behaviour is wrong,
BOOT_WAIT_WARM_MS is too short.

If GETCFG itself shows the wrong role, SETCFG/SAVE is broken.
"""

import utime
from machine import Pin, UART
from Drivers.uwb.bu03 import BU03

NODE_ID = 0
CHANNEL = 1
RATE    = 1
CYCLES  = 4   # how many anchor→tag→anchor cycles to run


def send_at_verbose(uart, command, delay_ms=1000):
    """Send AT command and return the full response string."""
    uart.write(command + "\r\n")
    utime.sleep_ms(delay_ms)
    resp = uart.read() if uart.any() else b""
    return resp.decode("utf-8", "replace").strip() if resp else "(no response)"


def read_role(config_uart):
    """Query GETCFG and parse the reported Role. Returns int or None."""
    resp = send_at_verbose(config_uart, "AT+GETCFG")
    print("  GETCFG response: {}".format(repr(resp)))
    for token in resp.replace(",", " ").split():
        if token.startswith("Role:"):
            try:
                return int(token.split(":")[1])
            except Exception:
                pass
    return None


def configure_and_verify(uwb, role, label):
    print("\n-- {} (role={}) --".format(label, role))
    t0 = utime.ticks_ms()
    uwb.configure_warm(NODE_ID, role=role, channel=CHANNEL, rate=RATE)
    elapsed = utime.ticks_diff(utime.ticks_ms(), t0)
    print("  configure_warm done in {} ms".format(elapsed))

    reported = read_role(uwb.config_uart)
    if reported is None:
        print("  ROLE CHECK: could not parse GETCFG")
    elif reported == role:
        print("  ROLE CHECK: OK (reported {})".format(reported))
    else:
        print("  ROLE CHECK: MISMATCH (expected {}, got {})".format(role, reported))
    return reported


print("=== UWB Role Cycle Diagnostic (node {}) ===\n".format(NODE_ID))

uwb = BU03(
    data_uart_id=1, data_tx=17, data_rx=18,
    config_uart_id=2, config_tx=2, config_rx=1,
    reset_pin=15,
)

print("Cold configure as anchor...")
t0 = utime.ticks_ms()
uwb.configure(NODE_ID, role=1, channel=CHANNEL, rate=RATE)
elapsed = utime.ticks_diff(utime.ticks_ms(), t0)
print("Cold configure done in {} ms".format(elapsed))

initial_role = read_role(uwb.config_uart)
print("Initial role reported: {}".format(initial_role))

for cycle in range(1, CYCLES + 1):
    print("\n===== CYCLE {} =====".format(cycle))
    configure_and_verify(uwb, role=0, label="Switch to TAG")
    configure_and_verify(uwb, role=1, label="Switch to ANCHOR")

print("\n=== Done ===")
