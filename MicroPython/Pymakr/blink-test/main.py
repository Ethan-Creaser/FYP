from machine import Pin, SPI, ADC
from neopixel import NeoPixel
import time
from sx127x import SX127x



# Setup NeoPixel

led_pin = 38
num_pixels = 1
np = NeoPixel(Pin(led_pin, Pin.OUT), num_pixels)



# Placeholder sensor (ADC)

sensor = ADC(Pin(1))



# Setup LoRa (SX1278 RA-02)

spi = SPI(
    2,
    baudrate=8000000,
    polarity=0,
    phase=0,
    sck=Pin(12),
    mosi=Pin(11),
    miso=Pin(13)
)

lora = SX127x(
    spi=spi,
    pins={
        "dio0": Pin(5),
        "reset": Pin(4),
        "ss": Pin(10)
    },
    parameters={
        "frequency": 915000000,
        "tx_power_level": 17,
        "signal_bandwidth": 125000,
        "spreading_factor": 7,
        "coding_rate": 5,
        "preamble_length": 8,
        "implicit_header": False,
        "sync_word": 0x12,
        "enable_crc": True
    }
)

#Set up LED
def cycle_led():
    """Cycle through red, green, blue."""
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    for r, g, b in colors:
        np[0] = (r, g, b)
        np.write()
        time.sleep(0.2)

# Set up functions
def read_sensor():
    """Return a simple sensor reading."""
    return sensor.read_u16()


def send_lora_message(msg):
    """Send a LoRa packet."""
    lora.println(msg)
    print("sent:", msg)


def check_lora_receive():
    """Check for incoming LoRa packets."""
    if lora.received_packet():
        incoming = lora.read_payload().decode()
        print("received:", incoming)
        # flash LED white on receive
        np[0] = (255, 255, 255)
        np.write()
        time.sleep(0.2)



# Main loop
while True:
    cycle_led()

    value = read_sensor()
    message = "sensor=" + str(value)
    send_lora_message(message)

    check_lora_receive()
