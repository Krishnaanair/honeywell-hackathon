# Third-party notices

EcoLoop Building Agents depends on open-source projects installed separately or
through Python package management. Their licences remain with their respective
copyright holders.

- EnergyPlus is a U.S. Department of Energy building simulation program. It is
  installed separately and its executable, libraries, and installer are not
  redistributed by this source repository. The repository does include the
  version-matched `HVACTemplate-5ZoneUnitaryHeatPump.idf` example, expanded
  without changing its building/HVAC design, under the EnergyPlus licence copied
  to `models/base/ENERGYPLUS_LICENSE.txt`. Source and checksums are recorded in
  `models/base/SOURCE.md` and `models/base/PROVENANCE.json`.
- The default Chicago TMY3 EPW is copied locally from the EnergyPlus 26.1.0
  distribution after checksum verification. The EPW is ignored by Git and is
  not included in the source archive. Its source and checksum are recorded in
  `weather/SOURCE.md`.
- The Model Context Protocol Python SDK is licensed under the MIT License.
- Ollama is distributed under its upstream licence. Model weights have their own
  licences; review the licence displayed by Ollama for the configured model.
- Direct runtime Python dependencies are HTTPX, the Model Context Protocol SDK,
  Ollama's Python client, pandas, pdfplumber, Plotly, Pydantic,
  pydantic-settings, pypdf, python-dotenv, ReportLab, Rich, Streamlit, and Typer.
  Development dependencies are mypy, pytest, pytest-asyncio, pytest-cov, Ruff,
  and typing stubs. Each package and its transitive dependencies retain their
  upstream licence.
- PowerPoint, LibreOffice, and Poppler are optional local rendering tools and are
  not redistributed.

Exact resolved Python versions and package source records are preserved in
`uv.lock`.
