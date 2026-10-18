"""Decode candump/ASC logs with the release-matched final protocol."""
import argparse,csv,sys
from pathlib import Path
import can,cantools
p=argparse.ArgumentParser();p.add_argument('log');p.add_argument('--output');p.add_argument('--message',default='');a=p.parse_args()
db=cantools.database.load_file(str(Path(__file__).parent/'protocol/bms_can1.dbc'),strict=True)
out=open(a.output,'w',newline='',encoding='utf-8') if a.output else sys.stdout
writer=csv.writer(out);writer.writerow(['time','id','message','signal','value','unit'])
try:
 for frame in can.LogReader(a.log):
  if frame.is_error_frame or frame.is_remote_frame or frame.is_fd:continue
  try:m=db.get_message_by_frame_id(frame.arbitration_id)
  except KeyError:continue
  if m.is_extended_frame!=frame.is_extended_id or len(frame.data)!=m.length or a.message not in m.name:continue
  values=m.decode(frame.data,decode_choices=False)
  units={s.name:s.unit or '' for s in m.signals}
  for name,value in values.items():writer.writerow([frame.timestamp,hex(frame.arbitration_id),m.name,name,value,units[name]])
finally:
 if out is not sys.stdout:out.close()
