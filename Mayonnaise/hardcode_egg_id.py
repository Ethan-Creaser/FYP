MAGIC    = 0xE9
NODE_ID  = 8
UWB_ID   = 2
UWB_ROLE = 1   # 0 = tag,  1 = anchor

role_str = "anchor" if UWB_ROLE else "tag"
print("Writing identity: node_id={}  uwb_id={}  uwb_role={}".format(
    NODE_ID, UWB_ID, role_str))

with open("identity.bin", "wb") as f:
    f.write(bytes([MAGIC, NODE_ID, UWB_ID, UWB_ROLE]))

with open("identity.bin", "rb") as f:
    data = f.read(4)

if (len(data) == 4 and data[0] == MAGIC
        and data[1] == NODE_ID and data[2] == UWB_ID and data[3] == UWB_ROLE):
    print("Verified OK: node_id={}  uwb_id={}  uwb_role={}".format(
        data[1], data[2], "anchor" if data[3] else "tag"))
else:
    print("VERIFY FAILED: read back {}".format(list(data)))
