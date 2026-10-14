# %% Setup
import csv, json, os, re
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import requests

DATA, OUT = Path("data"), Path("outputs")

BOROUGH = "London Borough of Tower Hamlets, London, UK"
CRS = "EPSG:27700"
WALK_SPEED_KMH = 4.8
WALK_MINUTES = 3            # Tower Hamlets' stated ambition: no resident more than a 3-minute walk
SENSITIVITY_MINUTES = 5     # also reported, to show how sensitive the result is to the threshold
RADIUS_M = WALK_SPEED_KMH * 1000 / 60 * WALK_MINUTES      # ~240 m along streets
RADIUS_5_M = WALK_SPEED_KMH * 1000 / 60 * SENSITIVITY_MINUTES   # ~400 m
COUNCIL_LAMP_COLUMN_POINTS = 2000   # council-reported lamp-column charge points, for validation
N_NEW_SITES = 5
CANDIDATE_SPACING_M = 100   # one candidate on-street site roughly every 100 m of residential street
DEDUPE_M = 30               # chargers closer than this are treated as the same site

ox.settings.use_cache = True
ox.settings.log_console = True

# %% 1. Borough, Output Areas, walking network
borough = ox.geocode_to_gdf(BOROUGH).to_crs(CRS)
poly = borough.geometry.iloc[0]
search = gpd.GeoSeries([poly.buffer(RADIUS_5_M + 100)], crs=CRS)
search_wgs = search.to_crs(4326).iloc[0]

oa_file = next(p for ext in (".geojson", ".gpkg", ".shp", ".zip") for p in DATA.glob(f"oa_boundaries*{ext}"))
oas = gpd.read_file(oa_file, mask=borough.to_crs(gpd.read_file(oa_file, rows=1).crs)).to_crs(CRS)
code_col = next(c for c in oas.columns if c.upper().startswith("OA21CD"))
oas = oas.rename(columns={code_col: "OA21CD"})[["OA21CD", "geometry"]]
oas = oas[oas.geometry.representative_point().within(poly)].reset_index(drop=True)
oas["centre"] = oas.geometry.representative_point()

G = ox.project_graph(ox.graph_from_polygon(search_wgs, network_type="walk"), to_crs=CRS)
oas["node"] = ox.distance.nearest_nodes(G, oas["centre"].x, oas["centre"].y)
print(f"Output Areas: {len(oas)}")

# %% 2. Census: which households depend on public charging?
def read_nomis(path):
    """Nomis query downloads wrap the table in title rows and footnotes.
    Returns a DataFrame with the real header row and only Output Area rows."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    is_oa = lambda r: any(re.fullmatch(r"E00\d{6}", x.strip()) for x in r)
    first = next(i for i, r in enumerate(rows) if is_oa(r))
    header = next(rows[i] for i in range(first - 1, -1, -1) if len([x for x in rows[i] if x.strip()]) > 2)
    data = [r for r in rows[first:] if is_oa(r)]
    df = pd.DataFrame(data, columns=header[:len(data[0])])
    code = next(c for c in df.columns if df[c].str.strip().str.fullmatch(r"E00\d{6}").all())
    df = df.rename(columns={code: "OA21CD"})
    for c in df.columns:
        if c != "OA21CD":
            df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce")
    return df.loc[:, ~df.columns.duplicated()]

def pick(df, *words, exclude=()):
    cols = [c for c in df.columns if all(w in c.lower() for w in words) and not any(e in c.lower() for e in exclude)]
    if not cols:
        raise KeyError(f"No column containing {words}. Columns are: {list(df.columns)}")
    return cols

cars = read_nomis(DATA / "census2021-ts045-oa.csv")
acc = read_nomis(DATA / "census2021-ts044-oa.csv")
print("TS045 columns:", list(cars.columns)); print("TS044 columns:", list(acc.columns))

for name, df in (("TS045", cars), ("TS044", acc)):
    if len([c for c in df.columns if c not in ("OA21CD", "mnemonic")]) < 3:
        raise KeyError(f"{name} only has one value column: re-download it from Nomis with ALL categories ticked")

tot_c = pick(cars, "total")[0]
no_car = pick(cars, "no cars")[0]
hh = pd.DataFrame({"OA21CD": cars["OA21CD"], "households": cars[tot_c],
                   "car_households": cars[tot_c] - cars[no_car]})

tot_a = pick(acc, "total")[0]
# Flat share = 1 - (houses + caravans) / total. Using the non-flat categories avoids depending on
# how Nomis labels the flat sub-types; aggregate columns are dropped so nothing is counted twice.
cats = [c for c in acc.columns if c not in ("OA21CD", tot_a) and acc[c].notna().all()]
leaf = [c for c in cats if not any(o != c and o.startswith(c) for o in cats)]
non_flat = [c for c in leaf if re.search(r"detached|terraced|caravan|mobile", c, re.I)]
if not non_flat:
    raise KeyError(f"TS044 has no house/caravan columns. Columns: {list(acc.columns)}")
acc["flat_share"] = 1 - acc[non_flat].sum(axis=1) / acc[tot_a]
assert acc["flat_share"].between(0, 1).all(), "Flat share outside 0-1; check the TS044 columns"
print(f"Non-flat categories used: {non_flat}")

hh = hh.merge(acc[["OA21CD", "flat_share"]], on="OA21CD")
# Census tables don't cross car ownership with dwelling type at this level, so we assume
# car ownership is spread evenly across dwelling types within each small area (stated limitation).
hh["dependent_households"] = hh["car_households"] * hh["flat_share"]
oas = oas.merge(hh, on="OA21CD", how="left")
assert oas["households"].notna().all(), "Some Output Areas missing from the census files"
dep_total = oas["dependent_households"].sum()
print(f"Households: {oas['households'].sum():,.0f}; with a car: {oas['car_households'].sum():,.0f}; "
      f"estimated to depend on public charging: {dep_total:,.0f}")

# %% 3. Public chargers: OpenStreetMap (+ optional hand-added sites), deduplicated
def flat(gdf):
    gdf = gdf.reset_index().rename(columns={"id": "osmid", "element": "element_type"})
    gdf["geometry"] = gdf.geometry.representative_point()
    return gdf

osm = flat(ox.features_from_polygon(search_wgs, tags={"amenity": "charging_station"})).to_crs(CRS)
if "access" in osm.columns:   # keep public chargers only
    osm = osm[~osm["access"].isin(["private", "customers", "no", "permit"])]
pts = [gpd.GeoDataFrame({"source": "OpenStreetMap", "name": osm.get("name")}, geometry=osm.geometry, crs=CRS)]
print(f"OpenStreetMap public charging sites: {len(osm)}")

# Open Charge Map: set the key with  export OCM_API_KEY=your_key  before running
key = os.environ.get("OCM_API_KEY")
if not key:
    raise SystemExit("Set OCM_API_KEY first:  export OCM_API_KEY=your_key")
minx, miny, maxx, maxy = gpd.GeoSeries([search_wgs], crs=4326).total_bounds
r = requests.get("https://api.openchargemap.io/v3/poi/", timeout=120, params={
    "output": "json", "key": key, "maxresults": 10000, "compact": "true", "verbose": "false",
    "boundingbox": f"({maxy},{minx}),({miny},{maxx})"})
r.raise_for_status()
raw = r.json()
ocm = pd.DataFrame([{"name": p.get("AddressInfo", {}).get("Title"),
                     "lat": p["AddressInfo"]["Latitude"], "lon": p["AddressInfo"]["Longitude"],
                     "points": p.get("NumberOfPoints") or 1} for p in raw])
ocm = gpd.GeoDataFrame(ocm, geometry=gpd.points_from_xy(ocm.lon, ocm.lat), crs=4326).to_crs(CRS)
ocm = ocm[ocm.within(search.iloc[0])]
ocm_in = ocm[ocm.within(poly)]
print(f"Open Charge Map: {len(raw)} locations returned, {len(ocm_in)} inside the borough "
      f"({int(ocm_in['points'].sum())} charge points listed)")
if len(raw) >= 10000:
    print("WARNING: hit the 10,000-result cap; some chargers may be missing")
pts.append(gpd.GeoDataFrame({"source": "Open Charge Map", "name": ocm["name"]}, geometry=ocm.geometry, crs=CRS))

manual = DATA / "chargers_manual.csv"
if manual.exists():
    m = pd.read_csv(manual)
    pts.append(gpd.GeoDataFrame({"source": "manual", "name": m["name"]},
                                geometry=gpd.points_from_xy(m.lon, m.lat), crs=4326).to_crs(CRS))

allpts = gpd.GeoDataFrame(pd.concat(pts, ignore_index=True), geometry="geometry", crs=CRS)
# Deduplicate: cluster points within DEDUPE_M of each other into one site
coords = np.c_[allpts.geometry.x, allpts.geometry.y]
site_id = -np.ones(len(allpts), dtype=int)
for i in range(len(allpts)):
    if site_id[i] < 0:
        near = np.where((np.hypot(*(coords - coords[i]).T) <= DEDUPE_M) & (site_id < 0))[0]
        site_id[near] = i
allpts["site"] = site_id
sites = allpts.groupby("site").agg(geometry=("geometry", "first"), name=("name", "first"),
                                   sources=("source", lambda x: ", ".join(sorted(set(x))))).reset_index(drop=True)
sites = gpd.GeoDataFrame(sites, geometry="geometry", crs=CRS)
sites["in_borough"] = sites.within(poly)
inb = sites[sites["in_borough"]]
src_counts = inb["sources"].value_counts().to_dict()
print(f"Charging sites after merging: {len(sites)} ({len(inb)} inside the borough); by source: {src_counts}")
print(f"Validation: council reports about {COUNCIL_LAMP_COLUMN_POINTS:,} lamp-column charge points; "
      f"Open Charge Map lists {int(ocm_in['points'].sum()):,} charge points in the borough")
sites_wgs = sites.to_crs(4326)
sites.assign(lat=sites_wgs.geometry.y.round(6), lon=sites_wgs.geometry.x.round(6)).drop(
    columns="geometry").to_csv(OUT / "charging_sites.csv", index=False)

# %% 4. Coverage against the council target (3 min), with 5 min as a sensitivity check
sites["node"] = ox.distance.nearest_nodes(G, sites.geometry.x, sites.geometry.y)
dist = nx.multi_source_dijkstra_path_length(G, set(sites["node"]), weight="length")
oas["walk_m"] = oas["node"].map(dist).fillna(np.inf)
oas["covered"] = oas["walk_m"] <= RADIUS_M
cov = oas.loc[oas["covered"], "dependent_households"].sum() / dep_total
cov_all = oas.loc[oas["covered"], "households"].sum() / oas["households"].sum()
cov5 = oas.loc[oas["walk_m"] <= RADIUS_5_M, "dependent_households"].sum() / dep_total
left_out = oas.loc[~oas["covered"], "dependent_households"].sum()
print(f"Dependent households within {WALK_MINUTES} min (council target): {cov:.1%} "
      f"(all households: {cov_all:.1%}); within {SENSITIVITY_MINUTES} min: {cov5:.1%}")
print(f"Estimated dependent households still beyond {WALK_MINUTES} min: {left_out:,.0f}")

# Biggest remaining gaps, for checking on a live charger map (e.g. Zapmap)
gaps = oas[~oas["covered"]].sort_values("dependent_households", ascending=False).head(15)
gp = gpd.GeoSeries(gaps["centre"], crs=CRS).to_crs(4326)
pd.DataFrame({"OA21CD": gaps["OA21CD"], "dependent_households": gaps["dependent_households"].round(),
              "walk_to_nearest_m": gaps["walk_m"].round(), "lat": gp.y.round(6), "lon": gp.x.round(6),
              "charger_actually_nearby": ""}).to_csv(OUT / "gap_check.csv", index=False)

# %% 5. Candidate on-street sites along residential streets
edges = ox.graph_to_gdfs(G, nodes=False).reset_index()
res = edges[edges["highway"].astype(str).str.contains("residential|living_street|tertiary|unclassified")]
res = res[res.intersects(poly)]
cand_pts = []
for geom in res.geometry:
    for d in np.arange(0, geom.length, CANDIDATE_SPACING_M):
        cand_pts.append(geom.interpolate(d))
cands = gpd.GeoDataFrame(geometry=gpd.GeoSeries(cand_pts, crs=CRS))
cands["node"] = ox.distance.nearest_nodes(G, cands.geometry.x, cands.geometry.y)
cands = cands.drop_duplicates("node").reset_index(drop=True)
print(f"Candidate on-street sites: {len(cands)}")

node_to_oas = oas.groupby("node").groups
def reach(n):
    r = nx.single_source_dijkstra_path_length(G, n, cutoff=RADIUS_M, weight="length")
    return {i for k in r for i in node_to_oas.get(k, [])}
cands["reach"] = cands["node"].apply(reach)

# %% 6. Greedy maximal coverage, weighted by dependent households
uncovered = set(oas.index[~oas["covered"]])
chosen = []
for rank in range(1, N_NEW_SITES + 1):
    gain = cands["reach"].apply(lambda r: oas.loc[list(r & uncovered), "dependent_households"].sum())
    best = gain.idxmax()
    if gain[best] <= 0:
        break
    uncovered -= cands.at[best, "reach"]
    chosen.append({"rank": rank, "households_added": round(gain[best]),
                   "coverage_after": 1 - oas.loc[list(uncovered), "dependent_households"].sum() / dep_total,
                   "geometry": cands.at[best, "geometry"]})
    cands = cands.drop(best)
new = gpd.GeoDataFrame(chosen, geometry="geometry", crs=CRS)
oas["newly_covered"] = oas.index.isin(set(oas.index[~oas["covered"]]) - uncovered)
new_wgs = new.to_crs(4326)
new.assign(lat=new_wgs.geometry.y.round(6), lon=new_wgs.geometry.x.round(6)).drop(
    columns="geometry").to_csv(OUT / "proposed_sites.csv", index=False)
print(new.drop(columns="geometry").to_string(index=False))

# %% 7. Map: need (dependent households) and access
fig, ax = plt.subplots(figsize=(9, 9))
oas["density"] = oas["dependent_households"] / (oas.area / 1e4)   # per hectare
gap = oas[~oas["covered"] & ~oas["newly_covered"]]
oas[oas["covered"]].plot(ax=ax, color="#c9d7ea", edgecolor="white", linewidth=0.2)
gap.plot(ax=ax, column="density", cmap="Reds", edgecolor="white", linewidth=0.2,
         vmin=0, vmax=oas["density"].quantile(0.95))
oas[oas["newly_covered"]].plot(ax=ax, color="#f3d27a", edgecolor="white", linewidth=0.2)
ox.graph_to_gdfs(G, nodes=False).clip(poly.buffer(50)).plot(ax=ax, color="#555", linewidth=0.2, alpha=0.3)
borough.boundary.plot(ax=ax, color="#1f497d", linewidth=1.2)
sites[sites["in_borough"]].plot(ax=ax, color="#1f497d", markersize=10, zorder=4, label="Public charging site")
if len(new):
    new.plot(ax=ax, color="#d35400", marker="*", markersize=220, edgecolor="black", linewidth=0.5,
             zorder=5, label="Proposed site")
    for _, r in new.iterrows():
        ax.annotate(str(r["rank"]), (r.geometry.x, r.geometry.y), xytext=(5, 5),
                    textcoords="offset points", fontsize=9, fontweight="bold", zorder=6)
from matplotlib.patches import Patch
h, _ = ax.get_legend_handles_labels()
h += [Patch(color="#c9d7ea", label=f"Within {WALK_MINUTES}-min walk (council target)"),
      Patch(color="#d9534f", label="Gap (darker = more car-owning flat households)"),
      Patch(color="#f3d27a", label="Covered by proposed sites")]
ax.legend(handles=h, loc="lower left", fontsize=8)
ax.set_axis_off()
ax.set_title(f"Is every resident within {WALK_MINUTES} minutes of a public charger? Tower Hamlets", fontsize=12, color="#1f497d", loc="left")
ax.annotate("Chargers: Open Charge Map and OpenStreetMap contributors. Streets © OpenStreetMap. Households: ONS Census 2021.",
            xy=(1, 0), xycoords="axes fraction", ha="right", fontsize=6, color="#666")
plt.tight_layout(); plt.savefig(OUT / "ev_access_map.png", dpi=250); plt.close(fig)

# %% 8. Results
results = {
    "households": int(oas["households"].sum()),
    "car_households": int(oas["car_households"].sum()),
    "dependent_households_est": int(dep_total),
    "charging_sites": int(len(sites)), "charging_sites_in_borough": int(sites["in_borough"].sum()),
    "sites_in_borough_by_source": src_counts,
    "ocm_charge_points_in_borough": int(ocm_in["points"].sum()),
    "council_reported_lamp_column_points": COUNCIL_LAMP_COLUMN_POINTS,
    "walk_minutes": WALK_MINUTES, "radius_m": round(RADIUS_M),
    "coverage_dependent_households": round(cov, 4), "coverage_all_households": round(cov_all, 4),
    "coverage_dependent_households_5min": round(cov5, 4),
    "dependent_households_beyond_target": int(left_out),
    "proposed_sites": new.drop(columns="geometry").to_dict(orient="records"),
    "coverage_after_new_sites": round(float(new["coverage_after"].iloc[-1]), 4) if len(new) else None,
}
(OUT / "results.json").write_text(json.dumps(results, indent=2, default=float))
print(json.dumps(results, indent=2, default=float))