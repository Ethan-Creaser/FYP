from Drivers.uwb.bu03 import BU03

uwb = BU03()

try:
    while True:
        distance = uwb.read_distance()
        print(f"Distance reading: {distance}")
        if distance is not None:
            print(f"Distance reading: {distance[0]} meters")
except KeyboardInterrupt:
    print("Stopped.")

