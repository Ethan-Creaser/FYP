from Drivers.uwb.bu03 import BU03
import utime


# Edit this list with the AT commands you want to send.
COMMANDS = [
    "AT",
    "AT+GETCFG",
    "AT+SETDEV=10,16290,1,0.018,0.642,1.0000,0.00,0,0",
    "AT+SAVE",
    # "AT+GETDEV"
    ]

DATA_UART_ID = 1
DATA_TX = 17
DATA_RX = 18
CONFIG_UART_ID = 2
CONFIG_TX = 2
CONFIG_RX = 1
RESET_PIN = 15

RESET_BEFORE_SEND = False
RESET_WARM = False
RESPONSE_TIMEOUT_MS = 1500
RESPONSE_IDLE_MS = 200
PAUSE_BETWEEN_COMMANDS_MS = 300


def reset_module(uwb, warm=False):
    uwb.reset_pin.value(0)
    utime.sleep_ms(500)
    uwb.reset_pin.value(1)
    utime.sleep_ms(2000 if warm else 4000)
    uwb._init_uarts()


def read_response(uart, timeout_ms=RESPONSE_TIMEOUT_MS, idle_ms=RESPONSE_IDLE_MS):
    response = bytearray()
    deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
    idle_deadline = None

    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if uart.any():
            chunk = uart.read()
            if chunk:
                response.extend(chunk)
                idle_deadline = utime.ticks_add(utime.ticks_ms(), idle_ms)
        elif idle_deadline is not None and utime.ticks_diff(idle_deadline, utime.ticks_ms()) <= 0:
            break

        utime.sleep_ms(20)

    return bytes(response)


def send_at(uwb, command):
    while uwb.config_uart.any():
        uwb.config_uart.read()

    print(">> {}".format(command))
    uwb.config_uart.write(command + "\r\n")

    response = read_response(uwb.config_uart)
    if not response:
        print("<< (no response)")
        print()
        return

    print("<< raw: {}".format(response))
    try:
        print("<< txt: {}".format(response.decode().strip()))
    except Exception:
        pass
    print()


def main():
    uwb = BU03(
        data_uart_id=DATA_UART_ID,
        data_tx=DATA_TX,
        data_rx=DATA_RX,
        config_uart_id=CONFIG_UART_ID,
        config_tx=CONFIG_TX,
        config_rx=CONFIG_RX,
        reset_pin=RESET_PIN,
    )

    if RESET_BEFORE_SEND:
        reset_module(uwb, warm=RESET_WARM)

    for command in COMMANDS:
        send_at(uwb, command)
        utime.sleep_ms(PAUSE_BETWEEN_COMMANDS_MS)


main()
