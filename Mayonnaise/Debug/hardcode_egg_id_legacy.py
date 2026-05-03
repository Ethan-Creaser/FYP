MAGIC   = 0xE9
NODE_ID = 6
UWB_ID  = 6

print("Writing identity: node_id={} uwb_id={}".format(NODE_ID, UWB_ID))
with open("identity.bin", "wb") as f:
    f.write(bytes([MAGIC, NODE_ID, UWB_ID]))

with open("identity.bin", "rb") as f:
    data = f.read(3)

if len(data) == 3 and data[0] == MAGIC and data[1] == NODE_ID and data[2] == UWB_ID:
    print("Verified OK: node_id={} uwb_id={}".format(data[1], data[2]))
else:
    print("VERIFY FAILED: read back {}".format(list(data)))
