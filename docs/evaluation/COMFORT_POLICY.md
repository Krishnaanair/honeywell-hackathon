# Comfort Policy

The full control policy is in [`../control-policy.md`](../control-policy.md).
Defaults are configuration values, not universal comfort standards.

Occupied limits:

- heating setpoint: 19–22 °C;
- cooling setpoint: 23–26 °C;
- operative target: 22–26 °C;
- minimum thermostat deadband: 2 °C;
- maximum normal setpoint change: 1 °C;
- absolute PMV target: at most 0.7;
- CO2 target, only when supported: at most 1000 ppm.

Unoccupied limits:

- heating setpoint: 17–21 °C;
- cooling setpoint: 24–29 °C;
- minimum deadband: 2 °C;
- maximum change: 2 °C.

The validator additionally enforces finite values, freshness, expiry, maximum
hold, actuator capability, monotonic generation, idempotency, demand emergency,
freeze/overheat protection and anti-chatter behavior. Both proposed and applied
values are retained whenever a clamp or repair occurs.
