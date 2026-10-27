const XLSX = require('xlsx');
const fs = require('fs');
const path = require('path');

const outDir = path.join(process.cwd(), 'qrt_datasets_pro');
fs.mkdirSync(outDir, { recursive: true });

function buildProRows(s) {
  return [
    // --- SECTION 1 : FONDS PROPRES DISPONIBLES (S.23.01) ---
    { 'Code EIOPA': 'S.23.01-R0500', 'Rubrique / Élément Solvabilité II': 'Fonds propres de base - Tier 1 non restreint', 'Montant Brut (C0040)': s.t1_unr, 'Montant Net (C0030)': s.t1_unr, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.23.01-R0500', 'Rubrique / Élément Solvabilité II': 'Fonds propres de base - Tier 1 restreint', 'Montant Brut (C0040)': s.t1_res, 'Montant Net (C0030)': s.t1_res, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.23.01-R0500', 'Rubrique / Élément Solvabilité II': 'Fonds propres - Tier 2 (Dettes subordonnées)', 'Montant Brut (C0040)': s.t2, 'Montant Net (C0030)': s.t2, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.23.01-R0500', 'Rubrique / Élément Solvabilité II': 'Fonds propres - Tier 3 (Impôts différés nets DTA)', 'Montant Brut (C0040)': s.t3, 'Montant Net (C0030)': s.t3, 'Unité': 'M€' },

    // --- SECTION 2 : RISQUE DE MARCHÉ (S.26.01) ---
    { 'Code EIOPA': 'S.26.01-R0110', 'Rubrique / Élément Solvabilité II': 'Risque de taux - Choc haussier (IR Up)', 'Montant Brut (C0040)': s.mkt_ir_up_brut, 'Montant Net (C0030)': s.mkt_ir_up_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0120', 'Rubrique / Élément Solvabilité II': 'Risque de taux - Choc baissier (IR Down)', 'Montant Brut (C0040)': s.mkt_ir_dn_brut, 'Montant Net (C0030)': s.mkt_ir_dn_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0200', 'Rubrique / Élément Solvabilité II': 'Risque actions (Equity Type 1 & 2)', 'Montant Brut (C0040)': s.mkt_eq_brut, 'Montant Net (C0030)': s.mkt_eq_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0300', 'Rubrique / Élément Solvabilité II': 'Risque immobilier (Property)', 'Montant Brut (C0040)': s.mkt_prop_brut, 'Montant Net (C0030)': s.mkt_prop_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0400', 'Rubrique / Élément Solvabilité II': 'Risque de spread (Obligations & Prêts)', 'Montant Brut (C0040)': s.mkt_sp_brut, 'Montant Net (C0030)': s.mkt_sp_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0500', 'Rubrique / Élément Solvabilité II': 'Risque de change (Currency FX)', 'Montant Brut (C0040)': s.mkt_fx_brut, 'Montant Net (C0030)': s.mkt_fx_net, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.01-R0600', 'Rubrique / Élément Solvabilité II': 'Risque de concentration de marché', 'Montant Brut (C0040)': s.mkt_conc_brut, 'Montant Net (C0030)': s.mkt_conc_net, 'Unité': 'M€' },

    // --- SECTION 3 : RISQUE DE DÉFAUT CONTREPARTIE (S.26.02) ---
    { 'Code EIOPA': 'S.26.02-R0100', 'Rubrique / Élément Solvabilité II': 'Défaut contrepartie - Type 1 (Réassureurs & Banques)', 'Montant Brut (C0040)': s.def_t1, 'Montant Net (C0030)': s.def_t1, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.02-R0200', 'Rubrique / Élément Solvabilité II': 'Défaut contrepartie - Type 2 (Intermédiaires & Assurés)', 'Montant Brut (C0040)': s.def_t2, 'Montant Net (C0030)': s.def_t2, 'Unité': 'M€' },

    // --- SECTION 4 : SOUSCRIPTION VIE (S.26.03) ---
    { 'Code EIOPA': 'S.26.03-R0100', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de mortalité', 'Montant Brut (C0040)': s.life_brut[0], 'Montant Net (C0030)': s.life_net[0], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0200', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de longévité', 'Montant Brut (C0040)': s.life_brut[1], 'Montant Net (C0030)': s.life_net[1], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0300', 'Rubrique / Élément Solvabilité II': 'Vie - Risque d invalidité / morbidité', 'Montant Brut (C0040)': s.life_brut[2], 'Montant Net (C0030)': s.life_net[2], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0400', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de rachat (Lapse)', 'Montant Brut (C0040)': s.life_brut[3], 'Montant Net (C0030)': s.life_net[3], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0500', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de frais généraux (Expense)', 'Montant Brut (C0040)': s.life_brut[4], 'Montant Net (C0030)': s.life_net[4], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0600', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de révision des rentes', 'Montant Brut (C0040)': s.life_brut[5], 'Montant Net (C0030)': s.life_net[5], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.03-R0700', 'Rubrique / Élément Solvabilité II': 'Vie - Risque de catastrophe vie', 'Montant Brut (C0040)': s.life_brut[6], 'Montant Net (C0030)': s.life_net[6], 'Unité': 'M€' },

    // --- SECTION 5 : SOUSCRIPTION SANTÉ (S.26.04) ---
    { 'Code EIOPA': 'S.26.04-R0100', 'Rubrique / Élément Solvabilité II': 'Santé similaire à la vie (SLT)', 'Montant Brut (C0040)': s.hlth_brut[0], 'Montant Net (C0030)': s.hlth_net[0], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.04-R0200', 'Rubrique / Élément Solvabilité II': 'Santé non similaire à la vie (NSLT)', 'Montant Brut (C0040)': s.hlth_brut[1], 'Montant Net (C0030)': s.hlth_net[1], 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.04-R0300', 'Rubrique / Élément Solvabilité II': 'Catastrophe Santé (Health Cat)', 'Montant Brut (C0040)': s.hlth_brut[2], 'Montant Net (C0030)': s.hlth_net[2], 'Unité': 'M€' },

    // --- SECTION 6 : SOUSCRIPTION NON-VIE (S.26.05) ---
    { 'Code EIOPA': 'S.26.05-R0100', 'Rubrique / Élément Solvabilité II': 'Non-Vie - Sous-risque Prime et Réserve', 'Montant Brut (C0040)': s.nl_prem_res, 'Montant Net (C0030)': s.nl_prem_res, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.05-R0200', 'Rubrique / Élément Solvabilité II': 'Non-Vie - Sous-risque Catastrophe (CAT)', 'Montant Brut (C0040)': s.nl_cat, 'Montant Net (C0030)': s.nl_cat, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.26.05-R0300', 'Rubrique / Élément Solvabilité II': 'Non-Vie - Sous-risque Résiliation (Lapse)', 'Montant Brut (C0040)': s.nl_lapse, 'Montant Net (C0030)': s.nl_lapse, 'Unité': 'M€' },

    // --- SECTION 7 : INCORPORELS, ASSIETTES SCR OPÉRATEUR & AJUSTEMENTS (S.25.01) ---
    { 'Code EIOPA': 'S.25.01-R0070', 'Rubrique / Élément Solvabilité II': 'Risque sur actifs incorporels (Intangibles)', 'Montant Brut (C0040)': s.intang, 'Montant Net (C0030)': s.intang, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0131', 'Rubrique / Élément Solvabilité II': 'Primes acquises brutes Vie (Assiette SCR op)', 'Montant Brut (C0040)': s.prem_vie, 'Montant Net (C0030)': s.prem_vie, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0132', 'Rubrique / Élément Solvabilité II': 'Primes acquises brutes Non-Vie (Assiette SCR op)', 'Montant Brut (C0040)': s.prem_nv, 'Montant Net (C0030)': s.prem_nv, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0133', 'Rubrique / Élément Solvabilité II': 'Provisions techniques Best Estimate Vie', 'Montant Brut (C0040)': s.be_vie, 'Montant Net (C0030)': s.be_vie, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0134', 'Rubrique / Élément Solvabilité II': 'Provisions techniques Best Estimate Non-Vie', 'Montant Brut (C0040)': s.be_nv, 'Montant Net (C0030)': s.be_nv, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0135', 'Rubrique / Élément Solvabilité II': 'Frais annuels contrats Unités de Compte (UC)', 'Montant Brut (C0040)': s.frais_uc, 'Montant Net (C0030)': s.frais_uc, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0460', 'Rubrique / Élément Solvabilité II': 'Provisions pour participation aux bénéfices (FDB - Plafond AdjTP)', 'Montant Brut (C0040)': s.fdb, 'Montant Net (C0030)': s.fdb, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.25.01-R0151', 'Rubrique / Élément Solvabilité II': 'Taux d impôt sur les sociétés (Taux IS)', 'Montant Brut (C0040)': s.taux_is, 'Montant Net (C0030)': s.taux_is, 'Unité': 'ratio' },
    { 'Code EIOPA': 'S.25.01-R0152', 'Rubrique / Élément Solvabilité II': 'Impôts différés passifs nets (DTL net - Plafond AdjDT)', 'Montant Brut (C0040)': s.dtl_net, 'Montant Net (C0030)': s.dtl_net, 'Unité': 'M€' },

    // --- SECTION 8 : MCR (S.28.01) ---
    { 'Code EIOPA': 'S.28.01-R0010', 'Rubrique / Élément Solvabilité II': 'MCR linéaire avant couloir [25% - 45% SCR]', 'Montant Brut (C0040)': s.mcr_lin, 'Montant Net (C0030)': s.mcr_lin, 'Unité': 'M€' },
    { 'Code EIOPA': 'S.28.01-R0330', 'Rubrique / Élément Solvabilité II': 'Seuil plancher absolu du MCR (AMCR)', 'Montant Brut (C0040)': s.amcr, 'Montant Net (C0030)': s.amcr, 'Unité': 'M€' },
  ];
}

const scenarios = [
  {
    file: '1_Assureur_Mixte_Solide_Pro.xlsx',
    t1_unr: 325.00, t1_res: 30.00, t2: 55.00, t3: 12.40,
    mkt_ir_up_brut: 54.20, mkt_ir_dn_brut: 31.80, mkt_eq_brut: 82.40, mkt_prop_brut: 34.60, mkt_sp_brut: 58.90, mkt_fx_brut: 16.50, mkt_conc_brut: 4.20,
    mkt_ir_up_net:  38.60, mkt_ir_dn_net:  24.10, mkt_eq_net:  59.30, mkt_prop_net:  25.80, mkt_sp_net:  41.70, mkt_fx_net:  12.40, mkt_conc_net:  3.10,
    def_t1: 15.80, def_t2: 11.40,
    life_brut: [13.50, 26.40, 6.20, 42.80, 18.90, 2.80, 9.40],
    life_net:  [10.80, 21.10, 4.90, 31.50, 15.20, 2.80, 7.60],
    hlth_brut: [14.20, 22.80, 7.50],
    hlth_net:  [11.60, 22.80, 7.50],
    nl_prem_res: 76.40, nl_cat: 45.20, nl_lapse: 12.60,
    intang: 2.50,
    prem_vie: 295.00, prem_nv: 380.00, be_vie: 1850.00, be_nv: 540.00, frais_uc: 5.60,
    fdb: 65.00, taux_is: 0.25, dtl_net: 75.00,
    mcr_lin: 89.40, amcr: 6.70,
  },
  {
    file: '2_Mutuelle_IARD_Dominante_Pro.xlsx',
    t1_unr: 228.50, t1_res: 18.00, t2: 95.00, t3: 42.00,
    mkt_ir_up_brut: 16.40, mkt_ir_dn_brut: 34.90, mkt_eq_brut: 46.20, mkt_prop_brut: 21.80, mkt_sp_brut: 31.50, mkt_fx_brut: 7.80, mkt_conc_brut: 5.10,
    mkt_ir_up_net:  15.20, mkt_ir_dn_net:  32.40, mkt_eq_net:  43.80, mkt_prop_net:  20.60, mkt_sp_net:  29.90, mkt_fx_net:  7.40, mkt_conc_net:  4.90,
    def_t1: 18.60, def_t2: 16.90,
    life_brut: [2.40, 7.20, 1.50, 4.80, 3.90, 5.40, 0.90],
    life_net:  [2.20, 6.80, 1.40, 4.40, 3.60, 5.40, 0.90],
    hlth_brut: [6.80, 34.50, 9.80],
    hlth_net:  [6.30, 34.50, 9.80],
    nl_prem_res: 118.50, nl_cat: 76.40, nl_lapse: 18.90,
    intang: 1.80,
    prem_vie: 28.00, prem_nv: 510.00, be_vie: 130.00, be_nv: 760.00, frais_uc: 0.00,
    fdb: 4.20, taux_is: 0.25, dtl_net: 18.50,
    mcr_lin: 115.00, amcr: 4.00,
  },
  {
    file: '3_Compagnie_Sous_Tension_Pro.xlsx',
    t1_unr: 118.00, t1_res: 38.00, t2: 135.00, t3: 52.00,
    mkt_ir_up_brut: 28.10, mkt_ir_dn_brut: 62.50, mkt_eq_brut: 74.80, mkt_prop_brut: 29.40, mkt_sp_brut: 55.60, mkt_fx_brut: 14.90, mkt_conc_brut: 8.70,
    mkt_ir_up_net:  25.80, mkt_ir_dn_net:  58.20, mkt_eq_net:  69.50, mkt_prop_net:  27.40, mkt_sp_net:  51.80, mkt_fx_net:  13.90, mkt_conc_net:  8.10,
    def_t1: 23.40, def_t2: 20.80,
    life_brut: [11.20, 21.60, 5.10, 39.40, 16.80, 3.10, 7.90],
    life_net:  [10.50, 20.40, 4.80, 36.90, 15.80, 3.10, 7.50],
    hlth_brut: [9.20, 19.40, 6.60],
    hlth_net:  [8.60, 19.40, 6.60],
    nl_prem_res: 98.20, nl_cat: 69.50, nl_lapse: 16.40,
    intang: 4.10,
    prem_vie: 175.00, prem_nv: 390.00, be_vie: 940.00, be_nv: 630.00, frais_uc: 2.80,
    fdb: 8.50, taux_is: 0.25, dtl_net: 3.20,
    mcr_lin: 72.00, amcr: 6.70,
  },
];

for (const sc of scenarios) {
  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(buildProRows(sc));
  XLSX.utils.book_append_sheet(wb, ws, 'Reporting_QRT_SolvencyII');
  XLSX.writeFile(wb, path.join(outDir, sc.file));
  console.log('Fichier PRO créé :', path.join(outDir, sc.file));
}
