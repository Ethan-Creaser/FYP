from Drivers.uwb.bu03 import BU03

uwb = BU03(uart_id=1, tx=17, rx=18)

print("Configuring UWB...")
uwb.reconfigure(0,1,1,1)
uwb.reset(1000)

try:
    while True:
        distance = uwb.read_distance()
        print(f"Distance reading: {distance}")
        if distance is not None:
            print(f"Distance reading: {distance[0]} meters")
except KeyboardInterrupt:
    print("Stopped.")
