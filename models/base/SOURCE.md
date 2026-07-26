# Building model provenance

## Selected model

- Repository file: `models/base/building.idf`
- Original example:
  `ExampleFiles/HVACTemplate-5ZoneUnitaryHeatPump.idf`
- Original distribution: EnergyPlus 26.1.0 official Windows x86-64 release,
  build `6f2e40d102`
- Original release:
  `https://github.com/NatLabRockies/EnergyPlus/releases/tag/v26.1.0`
- Source archive SHA-256:
  `0bb6932d277eed62f996b625f37c533b8c35f9af0c53710d961d8442fc4e70b3`
- Canonical expanded base-model SHA-256 (UTF-8 text with CRLF/CR normalized
  to LF):
  `cd25877a228fca41d8327004316e6046c65a6ef10514e61592710a2fe51b0a02`
- Licence: EnergyPlus licence copied to `ENERGYPLUS_LICENSE.txt`

## Selection rationale

The official version-matched example is a 463.6 m2, single-story, five-zone
office with 50 scheduled occupants and five separate unitary air-to-air heat
pump systems. Each system has direct-expansion cooling, a direct-expansion heat
pump heating coil, electric supplemental heating, outdoor air, an economizer,
and a zone thermostat with scheduled heating and cooling setpoints. It is
all-electric and compact enough for repeated smoke and demo runs.

## Deterministic expansion

The source uses EnergyPlus HVAC Template objects. To ensure the repository base
contains actual HVAC objects, it was expanded with the exact 26.1.0
`ExpandObjects` executable and `Energy+.idd`:

```text
copy HVACTemplate-5ZoneUnitaryHeatPump.idf <temporary-directory>/in.idf
copy Energy+.idd <temporary-directory>/Energy+.idd
<ENERGYPLUS_HOME>/ExpandObjects
copy <temporary-directory>/expanded.idf models/base/building.idf
```

The expanded model contains `ZoneControl:Thermostat`,
`Coil:Heating:DX:SingleSpeed`, `Coil:Cooling:DX:SingleSpeed`, and
`Coil:Heating:Electric` objects. No fuel coil or boiler object was found in the
expanded source. Model preparation validates these properties against the
installed 26.1.0 schema rather than relying only on this description. The
canonical checksum is independent of Git or operating-system line-ending
conversion and does not alter the IDF text seen by EnergyPlus.

Do not edit this base model in place. `prepare-model` writes controlled variants
under `models/generated/`.

## Thermal-comfort preparation assumptions

The official example's five `People` objects include an activity schedule but
omit the remaining Fanger inputs. Structured model preparation adds three
`Schedule:Constant` objects: external work efficiency 0.0, summer office
clothing insulation 0.5 clo, and air velocity 0.1 m/s. It selects the
`ClothingInsulationSchedule` method, uses enclosure-averaged mean radiant
temperature, and enables the Fanger comfort model. The generated preparation
manifest records these assumptions and the number of enabled `People` objects;
the versioned base model remains unchanged.
