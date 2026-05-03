"""Utility to write `identity.bin` for flashing eggs.

Edit `NODE_ID`/`UWB_ID` below then run this script on the target device
or via your flashing tool to persist the identity file.
"""

NODE_ID = 7
UWB_ID = 7

def _main():
    print("Writing identity: node_id={} uwb_id={}".format(NODE_ID, UWB_ID))
    try:
        from identity import write_identity, read_identity
        write_identity(NODE_ID, UWB_ID)
        data = read_identity()
        if data and data[0] == NODE_ID and data[1] == UWB_ID:
            print("Verified OK: node_id={} uwb_id={}".format(data[0], data[1]))
        else:
            print("VERIFY FAILED: read back {}".format(data))
    except Exception as e:
        print("Failed to write identity:", e)


if __name__ == '__main__':
    _main()