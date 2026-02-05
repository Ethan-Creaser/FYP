from machine import Pin, SPI
from time import sleep
from ulora.core import ULoRa  # Ensure the ULoRa class is implemented and imported correctly
#EX_LED = Pin(15, Pin.OUT)
# ============================================================================ 
# Sender Test Example
# ============================================================================ 
name_index = 4  # Change this index to select different names from the files
try:
    with open("egg_names.txt", "r") as f:
        # collect non-empty lines and strip whitespace

        lines = [line.strip() for line in f if line.strip()]
        print(f"Available names: {lines}")
        # get the second name (index 1) if available
        if len(lines) >= name_index:
            egg_name = lines[name_index]
        else:
            egg_name = "Unknown"
except Exception as e:
    print("Could not read eggs_names.txt:", e)
    egg_name = "Unknown"

if __name__ == "__main__": 
    # This example is designed for a MicroPython environment with an SX127x connected. 
    # Adjust the SPI bus and pin numbers as per your hardware configuration.
    try: 
        # ------------------------- Initializing SPI -------------------------
        print("Initializing SPI bus...")
        spi = SPI(1, baudrate=5000000, polarity=0, phase=0,
                  sck=Pin(12), mosi=Pin(11), miso=Pin(13))
        print(f"SPI bus initialized with SCK 12, MOSI: 11, MISO: 13.")
        
        # ------------------------- Defining Pin Mappings --------------------
        print("Setting up pin configurations...")
        pins = {
            "ss": 10,     # Chip Select (CS) pin
            "reset": 4,  # Reset pin
            "dio0": 5    # DIO0 pin
        }
        print(f"Pin configuration: SS={pins['ss']}, Reset={pins['reset']}, DIO0={pins['dio0']}.")
        
        # ------------------------- Creating ULoRa Instance ------------------
        print("Creating ULoRa instance with default parameters...")
        lora = ULoRa(spi, pins)
        print("ULoRa instance created successfully.")
        
        # ------------------------- Transmitting Test Message ----------------
        Counter = 0
        
        while True:
            try:
                message = input("Press Enter to send a test message... : ")
                print(f"User input received: '{message}'")
                if message == "":
                    message = "None"
            except Exception as e:
                print("Input error:", e)
                message = "None"  # Proceed to send message without waiting for input


            test_message = f"Hello From {egg_name}: {Counter}!"
            print("\n----- Transmitting Message -----")
            print(f"Message from {egg_name}: {message}")
            
            # Send the message via LoRa
            lora.println(test_message)
            
            print("Message transmission complete.")
            print("---------------------------------------------------------------------\n")
            Counter += 1
            #EX_LED.toggle()
             

    except Exception as e:
        # ------------------------- Error Handling --------------------------
        print("\nError during test:")
        print(f"Exception: {e}")
        print("Please check the wiring and LoRa module configuration.")