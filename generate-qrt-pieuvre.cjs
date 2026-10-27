const XLSX = require('xlsx');
const fs = require('fs');
const path = require('path');

const outDir = path.join(process.cwd(), 'qrt_pieuvre_scr');
fs.mkdirSync(outDir, { recursive: true });

const r2 = x => Math.round(x * 100) / 100;
const mat = (n, pairs) => {
  const M = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)));
  pairs.forEach(([i, j, v]) => { M[i][j] = v; M[j][i] = v; });
  return M;
};
const agg = (v, M) => Math.sqrt(Math.max(0, v.reduce((a, vi, i) => a + v.reduce((b, vj, j) => b + M[i][j] * vi * vj, 0), 0)));

// ---------- Matrices de correlation (formule standard) ----------
const MKT_NAMES = ['Taux', 'Actions', 'Immobilier', 'Spread', 'Change', 'Concentration'];
const MKT_UP = mat(6, [[1,2,.75],[1,3,.75],[1,4,.25],[2,3,.5],[2,4,.25],[3,4,.25]]);
const MKT_DN = mat(6, [[0,1,.5],[0,2,.5],[0,3,.5],[0,4,.25],[1,2,.75],[1,3,.75],[1,4,.25],[2,3,.5],[2,4,.25],[3,4,.25]]);
const LIFE_NAMES = ['Mortalité', 'Longévité', 'Invalidité', 'Rachat', 'Frais', 'Révision', 'Catastrophe'];
const LIFE_M = mat(7, [[0,1,-.25],[0,2,.25],[0,4,.25],[0,6,.25],[1,3,.25],[1,4,.25],[1,5,.25],[2,4,.5],[2,6,.25],[3,4,.5],[3,6,.25],[4,5,.5],[4,6,.25]]);
const HLTH_NAMES = ['SLT', 'NSLT', 'Catastrophe santé'];
const HLTH_M = mat(3, [[0,1,.5],[0,2,.25],[1,2,.25]]);
const NL_NAMES = ['Prime et réserve', 'Catastrophe', 'Résiliation'];
const NL_M = mat(3, [[0,1,.25],[0,2,.25],[1,2,.25]]);
const BSCR_NAMES = ['Marché', 'Défaut', 'Vie', 'Santé', 'Non-Vie'];
const BSCR_M = mat(5, [[0,1,.25],[0,2,.25],[0,3,.25],[0,4,.25],[1,2,.25],[1,3,.25],[1,4,.5],[2,3,.25]]);

function matrixRows() {
  const out = [];
  const push = (name, labels, M) => labels.forEach((a, i) => labels.forEach((b, j) => out.push({ Matrice: name, Ligne: a, Colonne: b, Correlation: M[i][j] })));
  push('BSCR', BSCR_NAMES, BSCR_M);
  push('Marche_Taux_Hausse', MKT_NAMES, MKT_UP);
  push('Marche_Taux_Baisse', MKT_NAMES, MKT_DN);
  push('Vie', LIFE_NAMES, LIFE_M);
  push('Sante', HLTH_NAMES, HLTH_M);
  push('NonVie', NL_NAMES, NL_M);
  return out;
}

// ---------- Calcul d'un module marche (choc taux retenu = pire scenario) ----------
function marketAgg(x) { // x = [irUp, irDn, eq, prop, sp, fx, conc]
  const rest = x.slice(2);
  const up = agg([x[0], ...rest], MKT_UP);
  const dn = agg([x[1], ...rest], MKT_DN);
  return dn > up ? { tot: dn, scen: 'Baisse', vals: [x[1], ...rest] } : { tot: up, scen: 'Hausse', vals: [x[0], ...rest] };
}
const defAgg = (t1, t2) => Math.sqrt(t1 * t1 + 1.5 * t1 * t2 + t2 * t2);

function build(s) {
  const mkB = marketAgg(s.mkt_brut), mkN = marketAgg(s.mkt_net);
  const defTot = defAgg(s.def_t1, s.def_t2);
  const M = {
    MKT:  { l: 'Risque de marché', col: '#1F77B4', ref: 'S.25.01.R0010', codes: ['IR','EQ','PROP','SP','FX','CONC'],
            labels: [`Taux d'intérêt (choc ${mkN.scen === 'Baisse' ? 'baissier' : 'haussier'} retenu)`, 'Actions', 'Immobilier', 'Spread', 'Change', 'Concentration'],
            b: mkB.vals, n: mkN.vals, bt: mkB.tot, nt: mkN.tot },
    DEF:  { l: 'Défaut de contrepartie', col: '#FF7F0E', ref: 'S.25.01.R0020', codes: ['T1','T2'],
            labels: ['Type 1 (réassureurs, banques)', 'Type 2 (intermédiaires, assurés)'],
            b: [s.def_t1, s.def_t2], n: [s.def_t1, s.def_t2], bt: defTot, nt: defTot },
    LIFE: { l: 'Souscription Vie', col: '#2CA02C', ref: 'S.25.01.R0030', codes: ['MORT','LONG','INV','LAPSE','EXP','REV','CAT'],
            labels: ['Mortalité','Longévité','Invalidité / morbidité','Rachat','Frais','Révision des rentes','Catastrophe vie'],
            b: s.life_brut, n: s.life_net, bt: agg(s.life_brut, LIFE_M), nt: agg(s.life_net, LIFE_M) },
    HLTH: { l: 'Souscription Santé', col: '#D62728', ref: 'S.25.01.R0040', codes: ['SLT','NSLT','CAT'],
            labels: ['Santé similaire à la vie (SLT)','Santé non similaire à la vie (NSLT)','Catastrophe santé'],
            b: s.hlth_brut, n: s.hlth_net, bt: agg(s.hlth_brut, HLTH_M), nt: agg(s.hlth_net, HLTH_M) },
    NL:   { l: 'Souscription Non-Vie', col: '#9467BD', ref: 'S.25.01.R0050', codes: ['PR','CAT','LAPSE'],
            labels: ['Prime et réserve','Catastrophe (NatCat + man-made)','Résiliation'],
            b: s.nl, n: s.nl, bt: agg(s.nl, NL_M), nt: agg(s.nl, NL_M) },
  };
  const keys = ['MKT', 'DEF', 'LIFE', 'HLTH', 'NL'];

  // BSCR
  const bscrCoreN = agg(keys.map(k => M[k].nt), BSCR_M);
  const bscrCoreB = agg(keys.map(k => M[k].bt), BSCR_M);
  const bscrN = r2(bscrCoreN + s.intang); // actifs incorporels ajoutés linéairement (Règl. délégué 2015/35, art. 204)
  const bscrB = r2(bscrCoreB + s.intang);

  // Operationnel, LAC TP, LAC DT
  const opPrem = 0.04 * s.prem_vie + 0.03 * s.prem_nv;
  const opProv = 0.0045 * s.be_vie + 0.03 * s.be_nv;
  const op = r2(Math.min(0.30 * bscrN, Math.max(opPrem, opProv)) + 0.25 * s.frais_uc);
  const lacTP = -r2(Math.min(s.fdb, s.tp_rate * bscrN));
  const lacDT = -r2(Math.min(s.dtl_net, s.taux_is * (bscrN + op + lacTP)));
  const scrN = r2(bscrN + op + lacTP + lacDT);
  const scrB = r2(bscrB + op + lacTP + lacDT);

  // ----- Feuille Pieuvre_SCR -----
  const rows = []; let ord = 0;
  const add = (niv, code, parent, branche, lib, b, n, ref, col) => rows.push({
    Niveau: niv, Code_Noeud: code, Code_Parent: parent, Branche: branche, 'Libellé': lib,
    'Brut avant mitigation (M€)': r2(b), 'Net retenu (M€)': r2(n), 'Effet mitigation (M€)': r2(n - b),
    'Poids dans SCR (%)': r2(Math.abs(n) / scrN * 100), Ordre_Tentacule: ++ord, Couleur_Hex: col, Ref_EIOPA: ref,
  });
  add(0, 'SCR', '', 'SCR', 'SCR total', scrB, scrN, 'S.25.01.R0200', '#000000');
  add(1, 'BSCR', 'SCR', 'BSCR', 'BSCR (SCR de base)', bscrB, bscrN, 'S.25.01.R0100', '#1B2A41');
  add(1, 'OP', 'SCR', 'OP', 'Risque opérationnel', op, op, 'S.25.01.R0130', '#7F7F7F');
  add(1, 'LAC_TP', 'SCR', 'LAC', 'Capacité d\'absorption – provisions techniques (FDB)', lacTP, lacTP, 'S.25.01.R0140', '#17BECF');
  add(1, 'LAC_DT', 'SCR', 'LAC', 'Capacité d\'absorption – impôts différés', lacDT, lacDT, 'S.25.01.R0150', '#BCBD22');

  let sumModN = 0, sumModB = 0;
  keys.forEach(k => {
    const m = M[k]; const mb = r2(m.bt), mn = r2(m.nt);
    add(2, k, 'BSCR', k, m.l, mb, mn, m.ref, m.col);
    sumModN += mn; sumModB += mb;
    let sb = 0, sn = 0;
    m.codes.forEach((c, i) => {
      const vb = r2(m.b[i]), vn = r2(m.n[i]); sb += vb; sn += vn;
      add(3, `${k}_${c}`, k, k, m.labels[i], vb, vn, '', m.col);
    });
    add(3, `DIV_${k}`, k, k, `Bénéfice de diversification – ${m.l}`, mb - sb, mn - sn, '', '#BFBFBF');
  });
  add(2, 'INT', 'BSCR', 'INT', 'Risque sur actifs incorporels', s.intang, s.intang, 'S.25.01.R0070', '#8C564B');
  sumModN += s.intang; sumModB += s.intang;
  add(2, 'DIV_BSCR', 'BSCR', 'BSCR', 'Bénéfice de diversification inter-modules', bscrB - sumModB, bscrN - sumModN, 'S.25.01.R0060', '#BFBFBF');

  // ----- S.25.01 officiel -----
  const R = (code, lib, c40, c30) => ({ 'Code EIOPA': code, 'Rubrique': lib, 'Brut (C0040)': r2(c40), 'Net (C0030)': r2(c30), 'Unité': 'M€' });
  const s25 = [
    R('S.25.01-R0010', 'Risque de marché', M.MKT.bt, M.MKT.nt),
    R('S.25.01-R0020', 'Risque de défaut de contrepartie', M.DEF.bt, M.DEF.nt),
    R('S.25.01-R0030', 'Risque de souscription Vie', M.LIFE.bt, M.LIFE.nt),
    R('S.25.01-R0040', 'Risque de souscription Santé', M.HLTH.bt, M.HLTH.nt),
    R('S.25.01-R0050', 'Risque de souscription Non-Vie', M.NL.bt, M.NL.nt),
    R('S.25.01-R0060', 'Diversification', bscrB - sumModB, bscrN - sumModN),
    R('S.25.01-R0070', 'Risque sur actifs incorporels', s.intang, s.intang),
    R('S.25.01-R0100', 'SCR de base (BSCR)', bscrB, bscrN),
    R('S.25.01-R0130', 'Risque opérationnel', op, op),
    R('S.25.01-R0140', 'Absorption par les provisions techniques', lacTP, lacTP),
    R('S.25.01-R0150', 'Absorption par les impôts différés', lacDT, lacDT),
    R('S.25.01-R0200', 'SCR', scrB, scrN),
  ];

  // ----- Fonds propres, MCR, ratios -----
  const t1 = s.t1_unr + Math.min(s.t1_res, 0.25 * s.t1_unr);
  const t3e = Math.min(s.t3, 0.15 * scrN);
  const t2e = Math.max(0, Math.min(s.t2, 0.5 * scrN - t3e));
  const eligSCR = t1 + t2e + t3e;
  const mcr = r2(Math.max(s.amcr, Math.min(Math.max(s.mcr_lin, 0.25 * scrN), 0.45 * scrN)));
  const eligMCR = t1 + Math.min(s.t2, 0.20 * mcr);
  const own = s.t1_unr + s.t1_res + s.t2 + s.t3;
  const F = (code, lib, v, u = 'M€') => ({ 'Code EIOPA': code, 'Rubrique': lib, 'Valeur': r2(v), 'Unité': u });
  const fp = [
    F('S.23.01-R0020', 'Tier 1 non restreint', s.t1_unr), F('S.23.01-R0030', 'Tier 1 restreint', s.t1_res),
    F('S.23.01-R0040', 'Tier 2 (dettes subordonnées)', s.t2), F('S.23.01-R0050', 'Tier 3 (DTA nets)', s.t3),
    F('S.23.01-R0060', 'Fonds propres disponibles totaux', own),
    F('S.23.01-R0070', 'Tier 1 éligible (limite restreint 20%)', t1), F('S.23.01-R0080', 'Tier 2 éligible', t2e), F('S.23.01-R0090', 'Tier 3 éligible (plafond 15% SCR)', t3e),
    F('S.23.01-R0100', 'Fonds propres éligibles pour couvrir le SCR', eligSCR),
    F('S.28.01-R0010', 'MCR linéaire', s.mcr_lin), F('S.28.01-R0330', 'Plancher absolu (AMCR)', s.amcr),
    F('S.28.01-R0310', 'MCR retenu (couloir 25%-45% du SCR)', mcr),
    F('S.28.01-R0340', 'Fonds propres éligibles pour couvrir le MCR', eligMCR),
    F('RATIO', 'Ratio de couverture SCR', eligSCR / scrN * 100, '%'),
    F('RATIO', 'Ratio de couverture MCR', eligMCR / mcr * 100, '%'),
  ];
  const syn = [
    { Indicateur: 'SCR net', Valeur: scrN, Unité: 'M€' }, { Indicateur: 'BSCR net', Valeur: bscrN, Unité: 'M€' },
    { Indicateur: 'Diversification inter-modules', Valeur: r2(bscrN - sumModN), Unité: 'M€' },
    { Indicateur: 'Risque opérationnel', Valeur: op, Unité: 'M€' }, { Indicateur: 'LAC TP', Valeur: lacTP, Unité: 'M€' }, { Indicateur: 'LAC DT', Valeur: lacDT, Unité: 'M€' },
    { Indicateur: 'Fonds propres éligibles SCR', Valeur: r2(eligSCR), Unité: 'M€' },
    { Indicateur: 'Ratio SCR', Valeur: r2(eligSCR / scrN * 100), Unité: '%' },
    { Indicateur: 'MCR', Valeur: mcr, Unité: 'M€' }, { Indicateur: 'Ratio MCR', Valeur: r2(eligMCR / mcr * 100), Unité: '%' },
    { Indicateur: 'Choc de taux retenu', Valeur: mkN.scen, Unité: '' },
  ];
  return { rows, s25, fp, syn };
}

// ---------- Scenarios (entrees) ----------
// mkt = [taux_hausse, taux_baisse, actions, immo, spread, change, concentration]
const scenarios = [
  { file: '1_Assureur_Mixte_Solide.xlsx',
    t1_unr: 325, t1_res: 30, t2: 55, t3: 12.4,
    mkt_brut: [54.2, 31.8, 82.4, 34.6, 58.9, 16.5, 4.2], mkt_net: [38.6, 24.1, 59.3, 25.8, 41.7, 12.4, 3.1],
    def_t1: 15.8, def_t2: 11.4,
    life_brut: [13.5, 26.4, 6.2, 42.8, 18.9, 2.8, 9.4], life_net: [10.8, 21.1, 4.9, 31.5, 15.2, 2.8, 7.6],
    hlth_brut: [14.2, 22.8, 7.5], hlth_net: [11.6, 22.8, 7.5], nl: [76.4, 45.2, 12.6],
    intang: 2.5, prem_vie: 295, prem_nv: 380, be_vie: 1850, be_nv: 540, frais_uc: 5.6,
    fdb: 65, tp_rate: 0.22, taux_is: 0.25, dtl_net: 75, mcr_lin: 89.4, amcr: 6.7 },
  { file: '2_Mutuelle_IARD_Dominante.xlsx',
    t1_unr: 228.5, t1_res: 18, t2: 95, t3: 42,
    mkt_brut: [16.4, 34.9, 46.2, 21.8, 31.5, 7.8, 5.1], mkt_net: [15.2, 32.4, 43.8, 20.6, 29.9, 7.4, 4.9],
    def_t1: 18.6, def_t2: 16.9,
    life_brut: [2.4, 7.2, 1.5, 4.8, 3.9, 5.4, 0.9], life_net: [2.2, 6.8, 1.4, 4.4, 3.6, 5.4, 0.9],
    hlth_brut: [6.8, 34.5, 9.8], hlth_net: [6.3, 34.5, 9.8], nl: [118.5, 76.4, 18.9],
    intang: 1.8, prem_vie: 28, prem_nv: 510, be_vie: 130, be_nv: 760, frais_uc: 0,
    fdb: 4.2, tp_rate: 0.03, taux_is: 0.25, dtl_net: 18.5, mcr_lin: 115, amcr: 4 },
  { file: '3_Compagnie_Sous_Tension.xlsx',
    t1_unr: 118, t1_res: 38, t2: 135, t3: 52,
    mkt_brut: [28.1, 62.5, 74.8, 29.4, 55.6, 14.9, 8.7], mkt_net: [25.8, 58.2, 69.5, 27.4, 51.8, 13.9, 8.1],
    def_t1: 23.4, def_t2: 20.8,
    life_brut: [11.2, 21.6, 5.1, 39.4, 16.8, 3.1, 7.9], life_net: [10.5, 20.4, 4.8, 36.9, 15.8, 3.1, 7.5],
    hlth_brut: [9.2, 19.4, 6.6], hlth_net: [8.6, 19.4, 6.6], nl: [98.2, 69.5, 16.4],
    intang: 4.1, prem_vie: 175, prem_nv: 390, be_vie: 940, be_nv: 630, frais_uc: 2.8,
    fdb: 8.5, tp_rate: 0.05, taux_is: 0.25, dtl_net: 3.2, mcr_lin: 72, amcr: 6.7 },
];

const addSheet = (wb, name, data, widths) => {
  const ws = XLSX.utils.json_to_sheet(data);
  ws['!cols'] = widths.map(w => ({ wch: w }));
  XLSX.utils.book_append_sheet(wb, ws, name);
};

for (const sc of scenarios) {
  const o = build(sc);
  const wb = XLSX.utils.book_new();
  addSheet(wb, 'Pieuvre_SCR', o.rows, [7, 14, 12, 8, 55, 22, 18, 20, 18, 16, 12, 16]);
  addSheet(wb, 'S2501_SCR', o.s25, [16, 45, 14, 14, 8]);
  addSheet(wb, 'Matrices_Correlation', matrixRows(), [22, 20, 20, 12]);
  addSheet(wb, 'Fonds_Propres_Ratios', o.fp, [16, 50, 12, 8]);
  addSheet(wb, 'Synthese', o.syn, [36, 14, 8]);
  XLSX.writeFile(wb, path.join(outDir, sc.file));
  console.log('Créé :', sc.file, '| SCR =', o.syn[0].Valeur, '| Ratio SCR =', o.syn[7].Valeur + '%');
}
