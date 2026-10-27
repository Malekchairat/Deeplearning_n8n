import * as XLSX from "xlsx";

export type ClimateProperty = { city: string; latitude: number; longitude: number; sum_insured_keur: number; flood_cover: 0 | 1; property_type?: string; annual_premium_keur?: number };

const ALIASES: Record<keyof ClimateProperty, string[]> = {
  city: ["city", "ville", "commune", "localisation", "adresse"],
  latitude: ["latitude", "lat"],
  longitude: ["longitude", "lon", "lng", "long"],
  sum_insured_keur: ["sum_insured_keur", "valeur_assuree_keur", "valeur_assuree", "capital_assure", "suminsured", "valeur"],
  flood_cover: ["flood_cover", "garantie_inondation", "inondation", "cat_nat", "catnat"],
  property_type: ["property_type", "type", "type_de_bien", "type_bien"],
  annual_premium_keur: ["annual_premium_keur", "prime_keur", "prime", "premium"],
};
const norm = (text: string) => text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
const num = (v: unknown) => (typeof v === "number" ? v : Number(String(v ?? "").replace(/\s/g, "").replace(",", ".")));
const yes = (v: unknown) => !["0", "non", "no", "false", "faux", "n"].includes(String(v ?? "1").trim().toLowerCase());

/** Reads a CSV / Excel book of insured properties (columns: ville, latitude, longitude, valeur assurée en k€, garantie inondation). */
export function parseClimatePortfolio(buffer: ArrayBuffer): { properties: ClimateProperty[]; skipped: number } {
  const wb = XLSX.read(buffer, { type: "array" });
  const sheet = wb.Sheets[wb.SheetNames[0]];
  if (!sheet) throw new Error("Le fichier ne contient aucune feuille.");
  const rows = XLSX.utils.sheet_to_json<Record<string, unknown>>(sheet, { defval: null, raw: true });
  const pick = (row: Record<string, unknown>, key: keyof ClimateProperty) => {
    const found = Object.keys(row).find((h) => ALIASES[key].includes(norm(h)));
    return found === undefined ? undefined : row[found];
  };
  const properties: ClimateProperty[] = [];
  let skipped = 0;
  for (const row of rows) {
    const city = String(pick(row, "city") ?? "").trim();
    const latitude = num(pick(row, "latitude")), longitude = num(pick(row, "longitude")), sum = num(pick(row, "sum_insured_keur"));
    if (!city || !Number.isFinite(latitude) || !Number.isFinite(longitude) || !(sum > 0) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) { skipped++; continue; }
    const premium = num(pick(row, "annual_premium_keur"));
    properties.push({ city, latitude, longitude, sum_insured_keur: sum, flood_cover: pick(row, "flood_cover") == null ? 1 : yes(pick(row, "flood_cover")) ? 1 : 0,
      property_type: String(pick(row, "property_type") ?? "").trim() || undefined, annual_premium_keur: Number.isFinite(premium) ? premium : undefined });
  }
  if (!properties.length) throw new Error("Aucune ligne exploitable : colonnes attendues « ville », « latitude », « longitude », « valeur assurée (k€) » et « garantie inondation » (0/1).");
  if (properties.length > 5000) throw new Error("Le portefeuille dépasse 5 000 lignes : regroupez les contrats par ville ou par zone.");
  return { properties, skipped };
}

export const CLIMATE_TEMPLATE_CSV = "ville;latitude;longitude;valeur_assuree_keur;garantie_inondation\nMarseille;43.2965;5.3698;250000;1\nParis;48.8566;2.3522;400000;1\nNice;43.7102;7.262;120000;0\n";
