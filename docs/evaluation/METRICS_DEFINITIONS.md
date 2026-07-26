# Metrics Definitions

All KPI formulas are implemented in `src/ecoloop/metrics.py` and publication
logic in `src/ecoloop/evaluation.py`. Frontend code does not redefine them.

- Electricity saving (%) =
  `100 × (baseline_kWh - controlled_kWh) / baseline_kWh`
- Peak reduction (%) =
  `100 × (baseline_peak_kW - controlled_peak_kW) / baseline_peak_kW`
- Cost = sum of interval `kWh × tariff_per_kWh`
- Operational carbon = sum of interval `kWh × kgCO2e_per_kWh`
- Occupied comfort compliance (%) =
  `100 × comfortable occupied duration / occupied duration`
- Violation degree-hours = sum of temperature distance outside the occupied
  operative-temperature band multiplied by interval hours.

EnergyPlus energy in joules is converted centrally using
`1 kWh = 3,600,000 J`. Power values in watts are converted to kilowatts and are
never summed as energy. Official totals come from EnergyPlus SQL/CSV outputs and
must pass a configured comparison against callback telemetry.

Other fuels are reported independently. Electricity savings are never relabelled
as total-building energy savings.
