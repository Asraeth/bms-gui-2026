from pathlib import Path
import cantools
if __name__=='__main__':
 for path in (Path(__file__).parent/'protocol').glob('*.dbc'):
  db=cantools.database.load_file(str(path),strict=True)
  print(f'{path.name}: {len(db.messages)} messages, strict validation passed')
