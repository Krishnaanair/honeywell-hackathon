# Default development weather provenance

The local development file `weather/default.epw` is copied from the official
EnergyPlus 26.1.0 Windows distribution:

```text
WeatherData/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw
```

- EnergyPlus release build: `6f2e40d102`
- Release archive SHA-256:
  `0bb6932d277eed62f996b625f37c533b8c35f9af0c53710d961d8442fc4e70b3`
- EPW SHA-256:
  `c7d4efcf93ba316a1d874352e743df5cf137ba5c0e3459eb2dc4b5442d5b7f5c`
- Licence: the licence included with the EnergyPlus distribution

The EPW itself is intentionally ignored by Git. Install and verify it from an
existing EnergyPlus 26.1.0 installation:

```powershell
py -3.11 scripts/install_weather.py --energyplus-home "$env:ENERGYPLUS_HOME"
```

If EnergyPlus has not yet been extracted, the same script can download the
exact official 26.1.0 release archive and verify both the published archive
checksum and EPW checksum before extracting only the weather member:

```powershell
py -3.11 scripts/install_weather.py --download
```

Alternatively set `ECOLOOP_WEATHER_PATH` to a verified, appropriately licensed
EPW.

This Chicago EPW is the reproducible locally installed default, not an
India-specific claim. An India/Chennai EPW may be used through configuration
only after its source, redistribution terms, and checksum have been verified.
