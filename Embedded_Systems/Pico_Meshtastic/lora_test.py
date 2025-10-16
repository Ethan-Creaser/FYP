# LoRa Communication with Raspberry Pi Pico - MicroPython
# Compatible with SX127x modules (like RFM95W, RFM96W)

from machine import Pin, SPI,PWM
import time
Ex_LED = Pin(15, Pin.OUT)


class LoRa:
    def __init__(self, spi_id=1, cs_pin=13, reset_pin=14, dio0_pin=16):
        """
        Initialize LoRa module
        Default pins for Raspberry Pi Pico:
        - CS: GPIO 17
        - Reset: GPIO 20  
        - DIO0: GPIO 16
        - SCK: GPIO 18 (SPI0)
        - MOSI: GPIO 19 (SPI0)
        - MISO: GPIO 16 (SPI0) - Note: shares with DIO0, or use different pin
        """
        
        # Initialize SPI
        self.spi = SPI(spi_id, baudrate=5000000, polarity=0, phase=0,
                      sck=Pin(10), mosi=Pin(11), miso=Pin(12))
        
        # Initialize control pins
        self.cs = Pin(cs_pin, Pin.OUT)
        self.reset = Pin(reset_pin, Pin.OUT)
        self.dio0 = Pin(dio0_pin, Pin.IN)
        
        # Set CS high initially
        self.cs.value(1)
        
        # Reset the module
        self.reset_module()
        
        # Initialize LoRa settings
        self.init_lora()
    
    def reset_module(self):
        """Reset the LoRa module"""
        self.reset.value(0)
        time.sleep_ms(10)
        self.reset.value(1)
        time.sleep_ms(10)
    
    def write_register(self, address, value):
        """Write to LoRa register"""
        self.cs.value(0)
        self.spi.write(bytearray([address | 0x80, value]))
        self.cs.value(1)
    
    def read_register(self, address):
        """Read from LoRa register"""
        self.cs.value(0)
        self.spi.write(bytearray([address & 0x7F]))
        response = self.spi.read(1)
        self.cs.value(1)
        return response[0]
    
    def init_lora(self):
        """Initialize LoRa module with basic settings"""
        
        # Put module in sleep mode
        self.write_register(0x01, 0x00)  # OpMode: Sleep
        
        # Set LoRa mode
        self.write_register(0x01, 0x80)  # OpMode: LoRa mode
        
        # Set frequency to 915MHz (adjust for your region)
        # For 433MHz use: 0x6C, 0x80, 0x00
        # For 868MHz use: 0xD9, 0x06, 0x66
        # For 915MHz use: 0xE4, 0xC0, 0x00
        self.write_register(0x06, 0xE4)  # FrfMsb
        self.write_register(0x07, 0xC0)  # FrfMid  
        self.write_register(0x08, 0x00)  # FrfLsb
        
        # Set spreading factor (SF7 = 128 chips/symbol)
        self.write_register(0x1E, 0x74)  # SF7, CRC on
        
        # Set bandwidth and coding rate
        self.write_register(0x1D, 0x72)  # BW=125kHz, CR=4/5
        
        # Set preamble length
        self.write_register(0x20, 0x00)  # PreambleMsb
        self.write_register(0x21, 0x08)  # PreambleLsb = 8
        
        # Set maximum payload length
        self.write_register(0x23, 0xFF)  # MaxPayloadLength
        
        # Set FIFO pointers
        self.write_register(0x0E, 0x00)  # FifoTxBaseAddr
        self.write_register(0x0F, 0x00)  # FifoRxBaseAddr
        
        print("LoRa module initialized successfully!")
        print(f"Version: 0x{self.read_register(0x42):02X}")
    
    def send_packet(self, data):
        """Send a packet via LoRa"""
        
        # Put in standby mode
        self.write_register(0x01, 0x81)  # OpMode: Standby
        
        # Clear IRQ flags
        self.write_register(0x12, 0xFF)  # IrqFlags
        
        # Set FIFO address pointer to FIFO TX base address
        self.write_register(0x0D, 0x00)  # FifoAddrPtr
        
        # Write payload length
        self.write_register(0x22, len(data))  # PayloadLength
        
        # Write data to FIFO
        self.cs.value(0)
        self.spi.write(bytearray([0x80]))  # FIFO write address
        if isinstance(data, str):
            self.spi.write(data.encode())
        else:
            self.spi.write(data)
        self.cs.value(1)
        
        # Start transmission
        self.write_register(0x01, 0x83)  # OpMode: TX
        
        # Wait for transmission to complete
        print("wait for transmission to complete")
        while True:
            irq_flags = self.read_register(0x12)
            print(irq_flags)
            if irq_flags & 0x08:  # TxDone
                break
            time.sleep_ms(1)
        
        # Clear IRQ flags
        self.write_register(0x12, 0xFF)
        
        print(f"Packet sent: {data}")
    
    def receive_packet(self, timeout_ms=5000):
        """Receive a packet via LoRa"""
        
        # Put in standby mode
        self.write_register(0x01, 0x81)  # OpMode: Standby
        
        # Clear IRQ flags
        self.write_register(0x12, 0xFF)  # IrqFlags
        
        # Set FIFO address pointer to FIFO RX base address
        self.write_register(0x0D, 0x00)  # FifoAddrPtr
        
        # Start reception
        self.write_register(0x01, 0x85)  # OpMode: RX Continuous
        
        start_time = time.ticks_ms()
        
        # Wait for packet reception or timeout
        while True:
            irq_flags = self.read_register(0x12)
            
            if irq_flags & 0x40:  # RxDone
                # Get packet length
                packet_length = self.read_register(0x13)  # RxNbBytes
                
                # Set FIFO address pointer to current RX address
                fifo_rx_current_addr = self.read_register(0x10)
                self.write_register(0x0D, fifo_rx_current_addr)
                
                # Read packet data
                self.cs.value(0)
                self.spi.write(bytearray([0x00]))  # FIFO read address
                packet_data = self.spi.read(packet_length)
                self.cs.value(1)
                
                # Get RSSI and SNR
                rssi = self.read_register(0x1A) - 157  # Adjust for frequency
                snr = self.read_register(0x19) / 4
                
                # Clear IRQ flags
                self.write_register(0x12, 0xFF)
                
                try:
                    decoded_data = packet_data.decode('utf-8')
                except:
                    decoded_data = packet_data
                
                print(f"Packet received: {decoded_data}")
                print(f"RSSI: {rssi} dBm, SNR: {snr} dB")
                
                return decoded_data, rssi, snr
            
            if irq_flags & 0x80:  # RxTimeout or CRC error
                print("Reception timeout or CRC error")
                self.write_register(0x12, 0xFF)  # Clear IRQ flags
                return None, None, None
            
            if time.ticks_diff(time.ticks_ms(), start_time) > timeout_ms:
                print("Reception timeout")
                return None, None, None
            
            time.sleep_ms(10)

# Example usage
def main():
    print("Initializing LoRa...")
    
    # Initialize LoRa module
    lora = LoRa()
    
    # Example: Simple sender
    def sender_example():
        print("Running sender example...")
        counter = 0
        while True:
            message = f"Hello LoRa! Count: {counter}"
            lora.send_packet(message)
            counter += 1
            time.sleep(5)
    
    # Example: Simple receiver
    def receiver_example():
        print("Running receiver example...")
        while True:
            print("Waiting for packet...")
            data, rssi, snr = lora.receive_packet(timeout_ms=10000)
            if data:
                print(f"Received: {data} (RSSI: {rssi}, SNR: {snr})")
            time.sleep(1)
    
    # Example: Ping-pong communication
    def ping_pong_example(is_master=True):
        print(f"Running ping-pong example ({'Master' if is_master else 'Slave'})")
        
        if is_master:
            counter = 0
            while True:
                # Send ping
                Ex_LED.toggle()
                message = f"PING {counter}"
                print(f"Sending: {message}")
                lora.send_packet(message)
                print(f"Sent: {message}")
                
                # Wait for pong
                data, rssi, snr = lora.receive_packet(timeout_ms=5000)
                if data and "PONG" in str(data):
                    print(f"Received: {data}")
                else:
                    print("No pong received")
                
                counter += 1
        else:
            while True:
                # Wait for ping
                data, rssi, snr = lora.receive_packet(timeout_ms=10000)
                if data and "PING" in str(data):
                    print(f"Received: {data}")
                    # Send pong
                    pong_message = f"PONG {data.split()[-1]}"
                    lora.send_packet(pong_message)
                    print(f"Sent: {pong_message}")
    
    # Choose example to run
    # sender_example()
    # receiver_example() 
    ping_pong_example(is_master=True)  # Set to False for slave

if __name__ == "__main__":
    main()