TESTING
=======

Quick instructions to run the two-egg hardware test and collect CSV logs.

1) Configure devices
   - Edit `config.json` on each device (or before flashing).
     - Egg1 (receiver): set `"node_id": 1`, `"node_name": "egg_1"`, `"hw_test_enabled": false`.
     - Egg2 (sender): set `"node_id": 2`, `"node_name": "egg_2"`, `"hw_test_enabled": true`, `"hw_test_target": 1`.
   - Tweak timing: recommended `hw_test_interval_s`: 6-10, `hw_test_ack_timeout_s`: 10-12.

2) Flash files to each board
   - Copy `config.json`, `main.py`, `node.py`, `hw_adapter.py`, `csv_logger.py`, and drivers to the board root.
   - Example (replace COM3):
     ```bash
     mpremote -p COM3 fs put config.json /config.json
     mpremote -p COM3 fs put main.py /main.py
     mpremote -p COM3 fs put node.py /node.py
     mpremote -p COM3 fs put hw_adapter.py /hw_adapter.py
     mpremote -p COM3 fs put csv_logger.py /csv_logger.py
     ```

3) Run
   - Reboot boards or run `main.py` from REPL. On the sender you should see `test: retry`/`timeout` logs and on the receiver `delivery ACK received` logs.

4) Collect logs
   - After a run, fetch CSV: `mpremote -p COM3 fs get /logs/packets.csv ./packets-eggX.csv`

5) Analyze
   - Open CSV in Excel or parse with Python to compute delivered vs timeouts.

Notes
-----
- CSV logging writes to `logs/packets.csv` on the device. For long runs consider pulling frequently to avoid flash wear.
- If you see many `BAD_RX` (packet too short + RX len=1), check wiring/antenna/grounding and consider lowering `spreading_factor` to reduce airtime.
