# Weather data

Place a licensed EnergyPlus weather file at `weather/default.epw` or set
`ECOLOOP_WEATHER_PATH` to another EPW. Weather files are not committed until
their source, redistribution terms, and SHA-256 checksum have been verified.

Run `python -m ecoloop doctor` after configuring the file.
