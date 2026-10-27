const XLSX = require('xlsx');
const fs = require('fs');
const path = require('path');

const outDir = path.join(process.cwd(), 'qrt_datasets');
fs.mkdirSync(outDir, { recursive: true });

function buildQrtRows(p) {
  return [
    // S.23.01.01 - Own Funds by Tier (Available Own Funds before Art. 82 capping)
    { code: 'S.23.01.01-R0500-C0020', libelle: 'Fonds propres de base disponibles - Tier 1 non restreint', valeur: p.t1_unr, unite: 'M€' },
    { code: 'S.23.01.01-R0500-C0030', libelle: 'Fonds propres de base disponibles - Tier 1 restreint', valeur: p.t1_res, unite: 'M€' },
    { code: 'S.23.01.01-R0500-C0040', libelle: 'Fonds propres disponibles - Tier 2 (dettes subordonnees)', valeur: p.t2, unite: 'M€' },
    { code: 'S.23.01.01-R0500-C0050', libelle: 'Fonds propres disponibles - Tier 3 (impots differes nets)', valeur: p.t3, unite: 'M€' },

    // S.26.01.01 - Market Risk Sub-Modules (Art. 164 - Dynamic parameter A = 0 if Up >= Down, else 0.5)
    { code: 'S.26.01.01-R0110', libelle: 'Risque de taux d interet - Choc haussier (Up)', valeur: p.ir_up, unite: 'M€' },
    { code: 'S.26.01.01-R0120', libelle: 'Risque de taux d interet - Choc baissier (Down)', valeur: p.ir_dn, unite: 'M€' },
    { code: 'S.26.01.01-R0200', libelle: 'Risque actions (Equity Type 1 & Type 2)', valeur: p.eq, unite: 'M€' },
    { code: 'S.26.01.01-R0300', libelle: 'Risque immobilier (Property)', valeur: p.prop, unite: 'M€' },
    { code: 'S.26.01.01-R0400', libelle: 'Risque de spread (Obligations, prets et titrisations)', valeur: p.sp, unite: 'M€' },
    { code: 'S.26.01.01-R0500', libelle: 'Risque de change (Currency FX)', valeur: p.fx, unite: 'M€' },
    { code: 'S.26.01.01-R0600', libelle: 'Risque de concentration de marche', valeur: p.conc, unite: 'M€' },

    // S.26.05.01 - Non-Life Underwriting Sub-Modules (Art. 114 - 3 official sub-modules)
    { code: 'S.26.05.01-R0100', libelle: 'Sous-risque de prime et de reserve Non-Vie', valeur: p.nl_pr, unite: 'M€' },
    { code: 'S.26.05.01-R0200', libelle: 'Sous-risque de rachat / resiliation Non-Vie (Lapse)', valeur: p.nl_lapse, unite: 'M€' },
    { code: 'S.26.05.01-R0300', libelle: 'Sous-risque de catastrophe Non-Vie (NatCat & Man-made)', valeur: p.nl_cat, unite: 'M€' },

    // S.25.01.21 - Core BSCR Modules, Intangibles, Operational Risk & Adjustments
    { code: 'S.25.01.21-R0020', libelle: 'Risque de defaut de contrepartie (Type 1 & Type 2)', valeur: p.def, unite: 'M€' },
    { code: 'S.25.01.21-R0030', libelle: 'Risque de souscription Vie', valeur: p.life, unite: 'M€' },
    { code: 'S.25.01.21-R0040', libelle: 'Risque de souscription Sante (SLT & NSLT)', valeur: p.health, unite: 'M€' },
    { code: 'S.25.01.21-R0070', libelle: 'Risque sur actifs incorporels (Intangible asset risk)', valeur: p.intang, unite: 'M€' },
    { code: 'S.25.01.21-R0130', libelle: 'Risque operationnel (Art. 204)', valeur: p.op, unite: 'M€' },
    { code: 'S.25.01.21-R0140', libelle: 'Capacite d absorption des pertes des provisions techniques (LAC TP)', valeur: p.lac_tp, unite: 'M€' },
    { code: 'S.25.01.21-R0150', libelle: 'Capacite d absorption des pertes des impots differes (LAC DT)', valeur: p.lac_dt, unite: 'M€' },

    // S.28.01.01 - Minimum Capital Requirement (MCR Linear & Absolute Floor Art. 248)
    { code: 'S.28.01.01-R0010', libelle: 'MCR lineaire avant tunnel [25% - 45% SCR]', valeur: p.mcr_lin, unite: 'M€' },
    { code: 'S.28.01.01-R0330', libelle: 'Seuil plancher absolu du MCR (AMCR)', valeur: p.amcr, unite: 'M€' },
  ];
}

const scenarios = [
  {
    file: '1_QRT_Composite_Solide_2025.xlsx',
    t1_unr: 168.0, t1_res: 15.0, t2: 28.0, t3: 6.0,
    ir_up: 32.0, ir_dn: 18.0, eq: 28.0, prop: 14.0, sp: 22.0, fx: 9.0, conc: 3.0,
    nl_pr: 34.0, nl_lapse: 6.0, nl_cat: 19.0,
    def: 11.4, life: 26.5, health: 14.8, intang: 0.0,
    op: 7.8, lac_tp: -6.4, lac_dt: -9.2,
    mcr_lin: 31.5, amcr: 6.7,
  },
  {
    file: '2_QRT_NonVie_TierCap_MCR45_2025.xlsx',
    t1_unr: 92.0, t1_res: 12.0, t2: 45.0, t3: 22.0,
    ir_up: 14.0, ir_dn: 26.0, eq: 24.0, prop: 11.0, sp: 19.0, fx: 6.0, conc: 2.5,
    nl_pr: 48.0, nl_lapse: 8.5, nl_cat: 29.0,
    def: 14.2, life: 8.0, health: 16.5, intang: 1.2,
    op: 8.4, lac_tp: -1.5, lac_dt: -5.8,
    mcr_lin: 58.0, amcr: 4.0,
  },
  {
    file: '3_QRT_Sous_Tension_Breach_Tiering_2025.xlsx',
    t1_unr: 52.0, t1_res: 20.0, t2: 65.0, t3: 30.0,
    ir_up: 19.0, ir_dn: 38.0, eq: 35.0, prop: 18.0, sp: 27.0, fx: 11.0, conc: 5.0,
    nl_pr: 52.0, nl_lapse: 11.0, nl_cat: 36.0,
    def: 19.5, life: 18.0, health: 12.0, intang: 2.0,
    op: 9.6, lac_tp: 0.0, lac_dt: -2.5,
    mcr_lin: 28.0, amcr: 6.7,
  },
];

for (const s of scenarios) {
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(buildQrtRows(s));
  XLSX.utils.book_append_sheet(wb, ws, 'QRT_Solvency_II');
  XLSX.writeFile(wb, path.join(outDir, s.file));
  console.log('Generated:', s.file);
}
