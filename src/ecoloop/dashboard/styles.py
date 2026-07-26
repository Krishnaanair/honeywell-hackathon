"""Visual system for the Streamlit evidence console."""

DASHBOARD_CSS = """
:root {
    --eco-ink: #102d2b;
    --eco-ink-soft: #294b48;
    --eco-muted: #647b78;
    --eco-green: #0b7f68;
    --eco-green-bright: #13aa7e;
    --eco-cyan: #167d91;
    --eco-amber: #bd7a18;
    --eco-line: #d9e6e2;
    --eco-line-strong: #c6d8d2;
    --eco-card: rgba(255, 255, 255, .97);
    --eco-shadow: 0 10px 32px rgba(19, 62, 56, .07);
}
[data-testid="stAppViewContainer"] {
    background:
        radial-gradient(circle at 92% -5%, rgba(19, 170, 126, .10), transparent 27rem),
        linear-gradient(180deg, #f8fbfa 0%, #f1f6f4 100%);
}
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, rgba(255,255,255,.99), rgba(246,250,248,.98));
    border-right: 1px solid var(--eco-line);
}
[data-testid="stSidebarContent"] { padding-top: 1.25rem; }
.block-container {
    max-width: 1440px;
    padding-top: 1.15rem;
    padding-bottom: 3rem;
}
.sidebar-brand {
    display: grid;
    gap: .08rem;
    padding: .3rem 0 .8rem;
}
.sidebar-brand span,
.section-heading span {
    color: var(--eco-green);
    font-size: .68rem;
    font-weight: 850;
    letter-spacing: .17em;
}
.sidebar-brand strong {
    color: var(--eco-ink);
    font-size: 1.18rem;
    letter-spacing: -.025em;
}
.ecoloop-hero {
    position: relative;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 2rem;
    min-height: 210px;
    padding: 2rem 2.2rem;
    margin-bottom: .9rem;
    border: 1px solid rgba(255,255,255,.12);
    border-radius: 22px;
    background:
        linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px),
        radial-gradient(circle at 86% 28%, rgba(72,220,171,.22), transparent 20rem),
        linear-gradient(120deg, #092723 0%, #0a4d43 58%, #0b6658 100%);
    background-size: 28px 28px, 28px 28px, auto, auto;
    box-shadow: 0 22px 64px rgba(9,54,47,.17);
}
.ecoloop-hero::after {
    content: "";
    position: absolute;
    right: -5rem;
    bottom: -10rem;
    width: 24rem;
    height: 24rem;
    border: 1px solid rgba(255,255,255,.15);
    border-radius: 50%;
    box-shadow: 0 0 0 3rem rgba(255,255,255,.035), 0 0 0 6rem rgba(255,255,255,.025);
}
.hero-copy {
    position: relative;
    z-index: 1;
    max-width: 760px;
}
.ecoloop-eyebrow { color: #8fe5c5; }
.ecoloop-hero h1 {
    margin: .38rem 0 .42rem;
    color: #fff !important;
    font-size: clamp(2.15rem, 4vw, 3.7rem);
    line-height: 1.03;
    letter-spacing: -.045em;
}
.ecoloop-hero p {
    margin: 0;
    color: #d6ebe5;
    font-size: 1.05rem;
    line-height: 1.55;
}
.hero-mode {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    gap: .75rem;
    min-width: 250px;
    padding: 1rem 1.05rem;
    border: 1px solid rgba(255,255,255,.22);
    border-radius: 16px;
    background: rgba(5,32,28,.46);
    backdrop-filter: blur(10px);
}
.hero-mode > div { display: grid; gap: .16rem; }
.hero-mode strong { color: #fff; font-size: .78rem; letter-spacing: .1em; }
.hero-mode small { color: #c8e1da; line-height: 1.35; }
.mode-signal {
    display: inline-block;
    flex: 0 0 auto;
    width: .66rem;
    height: .66rem;
    border: 2px solid rgba(255,255,255,.55);
    border-radius: 50%;
    background: #72e6b8;
    box-shadow: 0 0 0 .28rem rgba(114,230,184,.13);
}
.mode-replay .mode-signal,
.banner-replay .mode-signal {
    background: #8bd7ea;
    box-shadow: 0 0 0 .28rem rgba(139,215,234,.14);
}
.ecoloop-run-strip {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin: .45rem 0 .7rem;
    padding: .85rem 1rem;
    border: 1px solid var(--eco-line);
    border-radius: 14px;
    background: var(--eco-card);
    box-shadow: var(--eco-shadow);
}
.run-primary,
.run-secondary,
.run-title {
    display: flex;
    align-items: center;
    gap: .72rem;
    min-width: 0;
}
.run-primary { flex: 1; }
.run-secondary { flex: 0 0 auto; }
.ecoloop-run-strip code {
    overflow: hidden;
    color: var(--eco-muted);
    text-overflow: ellipsis;
    white-space: nowrap;
    background: transparent;
    font-size: .76rem;
}
.run-label,
.data-origin {
    color: var(--eco-muted);
    font-size: .64rem;
    font-weight: 800;
    letter-spacing: .11em;
}
.data-origin { color: var(--eco-green); }
.section-heading { margin: 1.45rem 0 1rem; }
.section-heading h2 {
    margin: .18rem 0 .25rem;
    color: var(--eco-ink) !important;
    font-size: clamp(1.75rem, 3vw, 2.35rem);
    line-height: 1.12;
    letter-spacing: -.04em;
}
.section-heading p {
    max-width: 850px;
    margin: 0;
    color: var(--eco-muted);
    font-size: .96rem;
    line-height: 1.55;
}
.subsection-heading { margin: 1.65rem 0 .75rem; }
.subsection-heading h3 {
    margin: 0 0 .16rem;
    color: var(--eco-ink) !important;
    font-size: 1.2rem;
}
.subsection-heading p { margin: 0; color: var(--eco-muted); font-size: .86rem; }
[data-testid="stMetric"] {
    min-height: 108px;
    padding: .9rem 1rem;
    border: 1px solid var(--eco-line);
    border-top: 3px solid #b8d8ce;
    border-radius: 14px;
    background: var(--eco-card);
    box-shadow: 0 7px 24px rgba(29,71,59,.05);
}
[data-testid="stMetricLabel"] {
    color: var(--eco-muted);
    font-size: .76rem;
    font-weight: 700;
}
[data-testid="stMetricValue"] {
    color: var(--eco-ink);
    font-weight: 790;
    letter-spacing: -.035em;
}
[data-testid="stMetricDelta"] { font-size: .72rem; }
.telemetry-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin: 0 0 1rem;
    padding: .72rem .9rem;
    border: 1px solid var(--eco-line);
    border-radius: 12px;
    background: #fff;
}
.telemetry-banner > div { display: flex; align-items: center; gap: .62rem; }
.telemetry-banner strong { color: var(--eco-ink); font-size: .72rem; letter-spacing: .09em; }
.telemetry-banner p { margin: 0; color: var(--eco-muted); font-size: .82rem; }
.banner-live { border-color: #a8dfce; background: linear-gradient(90deg,#f2fbf7,#fff); }
.banner-complete { border-color: #c5ddd6; background: linear-gradient(90deg,#f7faf9,#fff); }
.pair-strip {
    display: grid;
    grid-template-columns: minmax(0,1fr) auto minmax(0,1fr) auto;
    align-items: center;
    gap: 1rem;
    margin: .3rem 0 .85rem;
    padding: 1rem;
    border: 1px solid var(--eco-line);
    border-radius: 14px;
    background: var(--eco-card);
    box-shadow: var(--eco-shadow);
}
.pair-strip > div:not(.pair-arrow):not(.pair-meta) { display: grid; gap: .18rem; min-width: 0; }
.pair-strip span {
    color: var(--eco-muted);
    font-size: .63rem;
    font-weight: 800;
    letter-spacing: .1em;
}
.pair-strip strong {
    overflow: hidden;
    color: var(--eco-ink);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: .76rem;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.pair-arrow { color: var(--eco-green); font-size: 1.4rem; }
.pair-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .4rem; }
.pair-meta span {
    padding: .3rem .5rem;
    border-radius: 999px;
    background: #edf5f2;
    letter-spacing: .03em;
}
.outcome-banner {
    margin: .6rem 0 1rem;
    padding: .95rem 1rem;
    border: 1px solid var(--eco-line);
    border-left: 5px solid var(--eco-cyan);
    border-radius: 12px;
    background: #fff;
}
.outcome-banner span { display: block; color: var(--eco-ink); font-size: 1rem; font-weight: 780; }
.outcome-banner p { margin: .2rem 0 0; color: var(--eco-muted); font-size: .84rem; }
.outcome-positive { border-left-color: var(--eco-green-bright); background: #f5fcf9; }
.outcome-caution { border-left-color: var(--eco-amber); background: #fffaf1; }
[data-testid="stTabs"] [role="tablist"] {
    overflow-x: auto;
    padding: .32rem;
    background: rgba(255,255,255,.94);
    box-shadow: 0 5px 18px rgba(29,71,59,.04);
}
[data-testid="stTabs"] [role="tab"] {
    flex: 0 0 auto;
    height: 2.7rem;
    padding: 0 .9rem;
    font-size: .82rem;
    font-weight: 700;
}
[data-testid="stDataFrame"],
[data-testid="stPlotlyChart"] {
    overflow: hidden;
    border: 1px solid var(--eco-line);
    border-radius: 15px;
    background: #fff;
    box-shadow: 0 7px 25px rgba(29,71,59,.045);
}
.action-card { box-shadow: 0 6px 20px rgba(29,71,59,.04); }
.decision-summary {
    border-color: #bde1d5;
    border-left: 5px solid var(--eco-green);
    background:
        radial-gradient(circle at 95% 0, rgba(19,170,126,.10), transparent 15rem),
        linear-gradient(135deg,#f2fbf7,#fff);
}
.method-grid {
    display: grid;
    grid-template-columns: repeat(4,minmax(0,1fr));
    gap: .85rem;
    margin: 1rem 0;
}
.method-grid article {
    min-height: 172px;
    padding: 1rem;
    border: 1px solid var(--eco-line);
    border-radius: 14px;
    background: var(--eco-card);
    box-shadow: var(--eco-shadow);
}
.method-grid article span { color: var(--eco-green); font-size: .68rem; font-weight: 850; }
.method-grid article strong {
    display: block;
    margin: .72rem 0 .35rem;
    color: var(--eco-ink);
    font-size: 1.08rem;
}
.method-grid article p { margin: 0; color: var(--eco-muted); font-size: .82rem; line-height: 1.55; }
.ecoloop-footer { line-height: 1.65; }
[data-baseweb="select"] > div { border-color: var(--eco-line-strong); border-radius: 10px; }
@media (max-width: 1100px) {
    .ecoloop-hero { align-items: flex-start; flex-direction: column; }
    .hero-mode { min-width: min(100%,390px); }
    .pair-strip { grid-template-columns: minmax(0,1fr) auto minmax(0,1fr); }
    .pair-meta { grid-column: 1 / -1; justify-content: flex-start; }
    .method-grid { grid-template-columns: repeat(2,minmax(0,1fr)); }
}
@media (max-width: 760px) {
    .block-container { padding: .8rem; }
    .ecoloop-hero { min-height: 0; padding: 1.35rem; border-radius: 17px; }
    .ecoloop-hero h1 { font-size: 2.15rem; }
    .ecoloop-hero p { font-size: .92rem; }
    .hero-mode { width: 100%; min-width: 0; }
    .ecoloop-run-strip,
    .run-primary,
    .run-secondary,
    .run-title {
        align-items: flex-start;
        flex-direction: column;
    }
    .ecoloop-run-strip code { max-width: 78vw; }
    .telemetry-banner { align-items: flex-start; flex-direction: column; gap: .35rem; }
    .pair-strip { grid-template-columns: minmax(0,1fr); }
    .pair-arrow { transform: rotate(90deg); }
    .pair-meta { grid-column: auto; justify-content: flex-start; }
    .method-grid { grid-template-columns: minmax(0,1fr); }
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
    [data-testid="stHorizontalBlock"] > div { min-width: min(100%,280px); flex: 1 1 100%; }
}
"""
