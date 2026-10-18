"""The independent historical schema generator is retired."""
from pathlib import Path
if __name__=='__main__':
 print('Use firmware tools/generate.py and tools/sync_gui_protocol.py.')
 print('Current DBC:',Path(__file__).resolve().parents[1]/'protocol/bms_can1.dbc')
