"""Localisation application layer — mesh/UWB bridge.

This module is the join point between the mesh routing layer and the UWB
ranging / trilateration code.  The mesh routes bytes; this module interprets
them.

Teammate usage
--------------
Subclass LocaliseApp, override on_rx(), then attach it to the node:

    from app_localise import LocaliseApp, LOC_RANGE_REQ, LOC_RANGE_RESP

    class MyLoc(LocaliseApp):
        def on_rx(self, src_mesh_id, subtype, payload):
            if subtype == LOC_RANGE_REQ:
                # someone wants a range measurement from us — do UWB and reply
                distance = uwb.measure(src_mesh_id)
                self.send(src_mesh_id, LOC_RANGE_RESP, encode_float(distance))
            elif subtype == LOC_RANGE_RESP:
                # a range measurement arrived — feed it to trilateration
                trilat.add_measurement(src_mesh_id, decode_float(payload))

    loc = MyLoc(node)   # registers itself; mesh will call on_rx automatically

To send a ranging request to egg 5:
    loc.send(dst_mesh_id=5, subtype=LOC_RANGE_REQ)
"""

import constants

# ── Subtype constants ─────────────────────────────────────────────────────────
# These are suggestions — teammate can define their own or extend these.
LOC_RANGE_REQ  = 1   # request a UWB range measurement
LOC_RANGE_RESP = 2   # reply carrying the measured distance
LOC_POSITION   = 3   # broadcast computed position estimate


class LocaliseApp:
    def __init__(self, node):
        self.node = node
        node.localise_app = self   # registers with node so mesh calls on_rx

    def send(self, dst_mesh_id, subtype, payload=b""):
        """Send a localisation payload through the mesh.

        dst_mesh_id : mesh ID of the destination egg (1-14 or 99 for GS)
        subtype     : LOC_RANGE_REQ / LOC_RANGE_RESP / LOC_POSITION or custom
        payload     : raw bytes (your encoded measurement or request)
        """
        self.node.send_data(
            dst=dst_mesh_id,
            app_id=constants.APP_LOCALISE,
            subtype=subtype,
            data=payload,
        )

    def on_rx(self, src_mesh_id, subtype, payload):
        """Called by the mesh when a localisation packet arrives for this node.

        Teammate: override this method with your ranging/trilateration logic.

        src_mesh_id : mesh ID of the egg that sent this packet
        subtype     : message subtype
        payload     : raw bytes
        """
        print("[localise] RX from={} subtype={} len={}".format(
            src_mesh_id, subtype, len(payload)))
