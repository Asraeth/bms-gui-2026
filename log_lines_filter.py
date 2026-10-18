"""Filter a CAN log by the canonical physical-bus registry."""
import argparse,json
from pathlib import Path
import can
p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('output');p.add_argument('--bus',choices=['TSCAN','TSACCAN','CHRGCAN'],default='CHRGCAN');a=p.parse_args()
registry=json.loads((Path(__file__).parent/'protocol/registry.json').read_text())
ids={(m['can_id'],m['extended']) for m in registry['messages'] if m['bus']==a.bus}
with can.Logger(a.output) as writer:
 for frame in can.LogReader(a.input):
  if (frame.arbitration_id,frame.is_extended_id) in ids:writer(frame)
