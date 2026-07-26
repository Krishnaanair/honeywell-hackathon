# Baseline Methodology

The detailed current methodology is in [`../methodology.md`](../methodology.md).

The baseline is a conventional fixed-schedule BMS, not an intentionally weak
comparison. It uses 20/24 °C occupied thermostat schedules, 18/28 °C unoccupied
setback, the same HVAC enable behavior available in the selected model, and no
LLM, adaptive tariff response, or carbon response.

Baseline and controlled runs must share:

- source building physics, envelope, equipment and efficiencies;
- EnergyPlus 26.1.0 and timestep;
- weather content checksum;
- exact run-period dates and finalized simulation window;
- occupancy, internal loads, lighting and plug-load assumptions;
- tariff/carbon configuration;
- model-preparation fingerprint.

Only the actuated thermostat schedules and control events may differ. A
controlled run is locked to its parent baseline ID. Comparison is refused when
any required provenance, verified official metric, or energy cross-check is
missing.

The configured representative week is reported even when performance is worse.
The retained July week improved comfort but increased electricity; it is not
presented as a saving.
