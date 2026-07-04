# Commander Land Base Generator

Give it a commander name and it builds a full land base from Scryfall +
EDHREC data, never exceeding a requested total land count.

- Auto-includes Command Beacon and Ancient Tomb always, plus Command Tower
  for any commander with 2+ colors in its identity.
- Ranks the rest of the nonbasic pool by EDHREC inclusion rate.
- Fills remaining slots with basics.

## CLI

```
python get_lands.py "Korvold, Fae-Cursed King"
python get_lands.py "Korvold, Fae-Cursed King" --lands 38
```

## Web UI

```
pip install flask requests
python web_app.py
```

Then open http://127.0.0.1:5000.
