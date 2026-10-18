import csv,json,struct,time,unittest
from pathlib import Path
import can,cantools
from PySide6.QtWidgets import QApplication
from bmslib.model import Model
from bmslib.settings import SettingsClient,Response
import bms_gui

class CanonicalProtocolTest(unittest.TestCase):
 def test_physical_bus_uniqueness(self):
  registry=json.loads((Path(__file__).resolve().parents[1]/'protocol/registry.json').read_text())
  seen=set()
  for msg in registry['messages']:
   key=(msg['bus'],msg['can_id'],msg['extended'])
   self.assertNotIn(key,seen);seen.add(key)
   self.assertIn(msg['bus'],['TSCAN','TSACCAN','CHRGCAN']);self.assertLessEqual(msg['dlc'],8)
  self.assertGreater(len(seen),100)
 def test_strict_databases(self):
  for path in (Path(__file__).resolve().parents[1]/'protocol').glob('*.dbc'):
   db=cantools.database.load_file(str(path),strict=True)
   self.assertTrue(db.messages)
 def test_write_gates_bounds_tokens_and_malformed_responses(self):
  model=Model();sent=[];client=SettingsClient(model,lambda i,d:sent.append(d))
  client.write(1,60000);self.assertFalse(sent)
  client.observe_identity(1,2);client.write(1,60000);self.assertFalse(sent)
  client.observe_identity(2,2);client.write(1,model.settings_by_id[1].max+1);self.assertFalse(sent)
  client.write(1,60000);self.assertEqual(len(sent),1)
  token=sent[0][3];self.assertEqual(sent[0][2],2)
  self.assertIsNone(client.handle(b'\0'*7));self.assertTrue(client.busy())
  self.assertIsNone(client.handle(struct.pack('<BBBBi',2,1,0,((token%31+1)<<3),60000)));self.assertTrue(client.busy())
  client.handle(struct.pack('<BBBBi',2,1,0,token<<3,60000));self.assertFalse(client.busy())
  self.assertEqual(client.values[1],60000)
  self.assertIsNone(client.handle(struct.pack('<BBBBi',2,1,0,token<<3,0)));self.assertEqual(client.values[1],60000)

class SyntheticWindowTest(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
 def setUp(self):self.window=bms_gui.MainWindow(bms_gui.parse_args([]))
 def tearDown(self):self.window.close()
 def frame(self,name,**values):
  m=self.window.db.get_message_by_name(name);payload={s.name:0 for s in m.signals};payload.update(values)
  self.window._handle(can.Message(timestamp=time.time(),arbitration_id=m.frame_id,is_extended_id=m.is_extended_frame,data=m.encode(payload,strict=False)))
 def test_all_112_positions_raw_aux_health_and_faults(self):
  w=self.window;m=w.model;self.assertEqual(m.total_cells,112)
  self.frame('BMS_Identity',BMS_ProtocolVersion=2,BMS_ConfigVersion=2,BMS_BoardProfile=4,BMS_Slaves=14,BMS_CellsPerSlave=8,BMS_TempsPerSlave=8,BMS_PhysicalChannels=16)
  self.frame('BMS_Quality',BMS_CellsValid=1,BMS_AuxValid=1,BMS_CelsiusValid=1,BMS_IVTConfigured=1,BMS_CurrentValid=1,BMS_U1Valid=1,BMS_U2Valid=1,BMS_EnergyValid=1)
  for board in range(14):
   prefix=m.seg(board)
   for frame in range(2):
    message=w.db.get_message_by_frame_id(0xB000+board*2+frame)
    self.frame(message.name,**{m.cell_voltage(board,frame*4+j):3.4+board*0.01+j*0.001 for j in range(4)})
    message=w.db.get_message_by_frame_id(0xB080+board*2+frame)
    self.frame(message.name,**{m.cell_temp(board,frame*4+j):20+board+j*0.5 for j in range(4)})
    self.frame(prefix+f'_RawAux{frame+1}',**{prefix+f'_RawAux{frame*4+j+1:02d}':1.2+j*0.1 for j in range(4)})
   self.frame(prefix+'_Diagnostics',**{prefix+'_PEC_Count':7,prefix+'_Counter_Count':3,prefix+'_RawAux_ValidMask':255,prefix+'_Celsius_ValidMask':255})
  self.frame('BMS_Fast_1',Fast_Pack_Current=-12.345,Fast_Pack_Voltage=425.678)
  self.frame('BMS_Fast_2',Fast_Vehicle_Voltage=400.123,Fast_Power=-5254,Fast_Energy_Remaining=5000)
  self.frame('BMS_Energy',SoC=59.1,Energy_Used=345,Energy_Remaining=5000)
  self.frame('BMS_Limits',Discharge_Current_Limit=178.9,Regen_Current_Limit=20,Discharge_Power_Limit=80000)
  self.assertAlmostEqual(w.value('Fast_Pack_Current'),-12.345)
  self.assertAlmostEqual(w.value(m.cell_voltage(13,7)),3.533)
  self.assertAlmostEqual(w.value(m.cell_temp(13,7)),34.5)
  w._refresh_cards(time.time());w._refresh_diagnostics();self.assertEqual(w.diagnostics.rowCount(),112)
  for flag,name in m.faults.items():
   self.frame('BMS_Quality',BMS_FaultMask=flag)
   self.assertIn(name,w._faults_text()[0])
  for state in range(5):
   self.frame('BMS_Status',Precharge_State=state)
   self.assertIn(bms_gui.PRECHARGE_STATES[state],w._precharge_text())
  for state in range(6):
   self.frame('BMS_Charger',Charger_State=state,Charger_FaultMask=128,Charger_Voltage=450,Charger_Current=6.8)
   self.assertEqual(w.value('Charger_State'),state)
  self.frame('BMS_Quality',BMS_CellsValid=0,BMS_CelsiusValid=0)
  w._refresh_cards(time.time())
  self.assertEqual(w.value('BMS_CellsValid'),0)

 def test_actual_firmware_trace_reaches_gui(self):
  w=self.window;m=w.model
  with (Path(__file__).resolve().parents[1]/'protocol/firmware_trace.csv').open() as f:
   for row in csv.DictReader(f):
    if row['bus']=='1' and row['from_mcu']=='1':
     w._handle(can.Message(timestamp=time.time(),arbitration_id=int(row['id'],16),is_extended_id=row['ext']=='1',data=bytes.fromhex(row['data'])))
  for board in range(14):
   for cell in range(8):self.assertAlmostEqual(w.value(m.cell_voltage(board,cell)),3.70+board*0.004+cell*0.001,delta=0.002)
   for sensor in range(8):self.assertAlmostEqual(w.value(m.cell_temp(board,sensor)),20+board+sensor*0.5,delta=0.3)
  self.assertAlmostEqual(w.value('Fast_Pack_Current'),123.4,places=2)
  self.assertEqual(w.value('BMS_CellsValid'),1)
  self.assertEqual(w.value('BMS_FaultMask'),0)
  self.assertTrue(w.settings.compatible)
  w._refresh_cards(time.time());w._refresh_diagnostics()
 def test_bench_model_and_wrong_topology_write_inhibition(self):
  model=Model(str(Path(__file__).resolve().parents[1]/'bms_model_bench.json'))
  self.assertEqual(model.total_cells,8)
  sent=[];client=SettingsClient(model,lambda i,d:sent.append(d))
  client.observe_identity(2,2,14,8,8);client.write(1,60000);self.assertFalse(sent)
  client.observe_identity(2,2,1,8,8);client.write(1,60000);self.assertTrue(sent)
