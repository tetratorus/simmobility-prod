"""Thin glue between sg-urban-spine (Silver/Gold) and SimMobility Mid-Term, which runs OUTSIDE the spine.

Nothing here imports, builds, patches or redistributes SimMobility. The adapter only reshapes data around an
external run:

  export   Silver roads/districts/buildings/plots (EPSG:3414) -> SimMobility supply CSVs in the column layout written
           by dev/tools/roadNetworkFromOpenStreetMap_Python3.6/network.py (node/link/segment + polylines, EPSG:32648)
           plus taz / node_taz_map / address / postcode_node_map, and Silver `id_bridge` rows for every generated ID.
  (run)    an operator loads those CSVs into the SimMobility PostgreSQL schema and runs MidTerm (see rewire/README.md).
  publish  SimMobility outputs (subtrip_metrics.csv, link_travel_time.csv) -> Gold tables keyed by spine hub IDs,
           written with Write-Audit-Publish + an OpenLineage run event, exactly like `spine run` does for local runtimes.

CLI:  python adapter.py export  --lake LAKE --out SUPPLY_DIR [--scenario S] [--write-bridge]
      python adapter.py publish --lake LAKE --sim-output SIM_OUT_DIR [--scenario S]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

TRANSFORM_ID = "simmobility.mid_term"
SPINE_CRS = "EPSG:3414"
SIMMOB_CRS = "EPSG:32648"  # UTM 48N, used by the upstream SG network tooling (project.py UTM48N_PROJECTION)
SRC = "simmobility"

# Upstream rule (Build/Postcode_To_Node/02_Postcode_to_node.py): postcodes snap to the nearest node that is not a
# source/sink (1), not excluded (8, 9) and not a bus terminus.
EXCLUDED_NODE_TYPES = (1, 8, 9)
# Upstream OSM conversion (process_osm.py): highway tag -> link category, default speed (km/h).
HIGHWAY_CATEGORY = {"motorway": 1, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4, "motorway_link": 6,
                    "trunk_link": 6, "primary_link": 6, "secondary_link": 6, "tertiary_link": 6}
HIGHWAY_SPEED_KMH = {"motorway": 100, "trunk": 90, "primary": 80, "secondary": 70, "tertiary": 50,
                     "motorway_link": 100, "trunk_link": 90, "primary_link": 80, "secondary_link": 70,
                     "tertiary_link": 50}
# metadata/LinkCat_Roadtype_LaneWidth.csv: CategoryID -> Roadtype
CATEGORY_ROAD_TYPE = {1: 1, 2: 2, 3: 2, 4: 5, 5: 5, 6: 3, 7: 4}
# process_osm.py DEFAULT_SEGMENT_ATTR
DEFAULT_CATEGORY, DEFAULT_SPEED_KMH, DEFAULT_CAPACITY, DEFAULT_LANES = 2, 60, 1000, 1

# Person::serializeSubTripChainItemTravelTimeMetrics (shared/entities/Person.cpp); restricted-region (CBD)
# columns follow and are ignored here.
SUBTRIP_COLUMNS = ["person_id", "trip_id", "subtrip_id", "origin_type", "origin_id", "origin_taz",
                   "destination_type", "destination_id", "destination_taz", "mode", "start_time", "end_time",
                   "travel_time", "total_distance"]  # travel_time in hours, total_distance in metres
# LinkTravelTime::dumpTravelTimesToFile (shared/entities/TravelTimeManager.cpp), ';'-separated, seconds.
LINK_TT_COLUMNS = ["link_id", "downstream_link_id", "start_time", "end_time", "travel_time_s"]

DEFAULTS = {"simmobility_crs": SIMMOB_CRS, "node_snap_m": 0.5, "two_way": True, "accessibility_threshold_min": 45.0}


# ---------------------------------------------------------------------------------------------------------------- utils
def _geo(df, crs=SPINE_CRS):
    import geopandas as gpd

    g = df if isinstance(df, gpd.GeoDataFrame) else gpd.GeoDataFrame(df, geometry="geometry")
    return g.set_crs(SPINE_CRS) if g.crs is None else g.to_crs(crs) if crs else g


def _version(df) -> str:
    if df is not None and "source_version" in df.columns and df["source_version"].notna().any():
        return "silver:" + ",".join(sorted(map(str, df["source_version"].dropna().unique())))
    return "silver:unversioned"


def _bridge(source, ids, hub, hub_ids, method, confidence, version) -> pd.DataFrame:
    return pd.DataFrame({"source": f"{SRC}.{source}", "source_id": [str(i) for i in ids], "hub": hub,
                         "hub_id": [str(h) for h in hub_ids], "match_method": method,
                         "confidence": np.asarray(confidence, dtype=float) * np.ones(len(ids)),
                         "source_version": version})


def _prior(bridge, source) -> pd.DataFrame:
    if bridge is None or len(bridge) == 0:
        return pd.DataFrame(columns=["source_id", "hub_id"])
    b = bridge[bridge["source"] == f"{SRC}.{source}"]
    return b.assign(source_id=b["source_id"].astype(int))[["source_id", "hub_id"]]


def valid_nodes(nodes: pd.DataFrame) -> pd.DataFrame:
    keep = ~nodes["node_type"].isin(EXCLUDED_NODE_TYPES)
    if "bus_terminus_node" in nodes.columns:
        keep &= ~nodes["bus_terminus_node"].astype(bool)
    return nodes[keep]


def nearest_valid_node(points_xy: np.ndarray, nodes: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Nearest eligible node (upstream postcode->node rule). nodes: node_id, node_type, x, y [, bus_terminus_node]."""
    from shapely import STRtree, points

    v = valid_nodes(nodes)
    tree = STRtree(points(v[["x", "y"]].to_numpy(float)))
    idx, dist = tree.query_nearest(points(np.asarray(points_xy, float)), return_distance=True, all_matches=False)
    return v["node_id"].to_numpy()[idx[1]], dist


# --------------------------------------------------------------------------------------------------------------- export
def export_supply(inputs: dict, params: dict | None = None) -> dict[str, pd.DataFrame]:
    """Silver -> SimMobility supply tables + id_bridge rows. Inputs: roads (required), districts, buildings, plots,
    id_bridge (optional; prior simmobility.* rows keep link and TAZ IDs stable across exports)."""
    p = {**DEFAULTS, **(params or {})}
    crs, snap = p["simmobility_crs"], float(p["node_snap_m"])
    roads = _geo(inputs["roads"], crs).sort_values("road_id")
    version = _version(inputs["roads"])
    prior = inputs.get("id_bridge")

    parts = []
    for r in roads.itertuples():
        geoms = list(r.geometry.geoms) if r.geometry.geom_type.startswith("Multi") else [r.geometry]
        for g in geoms:
            xy = np.asarray(g.coords)[:, :2]
            if len(xy) >= 2 and g.length > 0:
                parts.append((r.road_id, getattr(r, "road_class", None), getattr(r, "name", None), xy))

    node_ix: dict[tuple, int] = {}
    node_xy: list[np.ndarray] = []

    def node(pt):
        k = (round(pt[0] / snap), round(pt[1] / snap))
        if k not in node_ix:
            node_ix[k] = len(node_xy) + 1
            node_xy.append(pt)
        return node_ix[k]

    old_links = _prior(prior, "link")
    by_road = old_links.groupby("hub_id")["source_id"].apply(sorted).to_dict()
    next_id = int(old_links["source_id"].max()) + 1 if len(old_links) else 1
    used: dict[str, int] = {}
    links, link_pl, segs, seg_pl = [], [], [], []
    for road_id, cls, name, xy in parts:
        cat = HIGHWAY_CATEGORY.get(str(cls), DEFAULT_CATEGORY)
        speed = HIGHWAY_SPEED_KMH.get(str(cls), DEFAULT_SPEED_KMH)
        directions = [xy, xy[::-1]] if p["two_way"] else [xy]
        for d in directions:
            n = used.get(road_id, 0)
            used[road_id] = n + 1
            prev = by_road.get(str(road_id), [])
            if n < len(prev):
                lid = prev[n]
            else:
                lid, next_id = next_id, next_id + 1
            a, b = node(d[0]), node(d[-1])
            links.append({"id": lid, "road_type": CATEGORY_ROAD_TYPE[cat], "category": cat, "from_node": a,
                          "to_node": b, "road_name": name or "", "tags": "", "osmid": -1, "_road_id": road_id})
            sid = lid * 10 + 1
            segs.append({"id": sid, "link_id": lid, "sequence": 1, "num_lanes": DEFAULT_LANES,
                         "capacity": DEFAULT_CAPACITY, "max_speed": speed, "tags": "", "link_category": cat,
                         "_road_id": road_id})
            for s, (x, y) in enumerate(d, start=1):
                link_pl.append({"id": lid, "x": x, "y": y, "z": 0, "seq_id": s})
                seg_pl.append({"id": sid, "x": x, "y": y, "z": 0, "seq_id": s})

    links = pd.DataFrame(links)
    segs = pd.DataFrame(segs)
    degree = pd.concat([links.from_node, links.to_node]).value_counts()
    nxy = np.asarray(node_xy)
    nodes = pd.DataFrame({"id": np.arange(1, len(nxy) + 1), "x": nxy[:, 0], "y": nxy[:, 1], "z": 0,
                          "traffic_light_id": 0, "tags": ""})
    deg = nodes["id"].map(degree).fillna(0).to_numpy()
    # Node.hpp NodeType: 1 source/sink (dead end), 2 intersection, 0 default (mid-road join)
    nodes["node_type"] = np.where(deg <= 2, 1, np.where(deg >= 6 if p["two_way"] else deg >= 3, 2, 0))
    nodes["osmid"] = -1

    out = {"node": nodes, "link": links.drop(columns="_road_id"), "link_polyline": pd.DataFrame(link_pl),
           "segment": segs.drop(columns="_road_id"), "segment_polyline": pd.DataFrame(seg_pl)}
    bridges = [_bridge("link", links.id, "road", links._road_id, "exact", 1.0, version),
               _bridge("segment", segs.id, "road", segs._road_id, "exact", 1.0, version)]

    districts = inputs.get("districts")
    if districts is not None and len(districts):
        import geopandas as gpd

        d = _geo(districts, crs).sort_values("district_id")
        old_taz = _prior(prior, "taz")
        known = dict(zip(old_taz.hub_id, old_taz.source_id))
        nxt = (max(known.values()) + 1) if known else 1
        codes = []
        for did in d.district_id.astype(str):
            if did not in known:
                known[did], nxt = nxt, nxt + 1
            codes.append(known[did])
        d = d.assign(taz_code=codes)
        out["taz"] = pd.DataFrame({"taz_code": d.taz_code, "district_id": d.district_id.astype(str)})
        bridges.append(_bridge("taz", d.taz_code, "district", d.district_id, "exact", 1.0, _version(districts)))

        def zone_of(pts: pd.DataFrame) -> pd.DataFrame:
            g = gpd.GeoDataFrame(pts, geometry=gpd.points_from_xy(pts.x, pts.y), crs=crs)
            j = gpd.sjoin_nearest(g, d[["taz_code", "district_id", "geometry"]], how="left", distance_col="_d")
            j = j[~j.index.duplicated()]
            return pts.assign(taz_code=j.taz_code.to_numpy(), district_id=j.district_id.astype(str).to_numpy(),
                              _inside=(j._d.to_numpy() == 0))

        nz = zone_of(nodes[["id", "x", "y"]])
        out["node_taz_map"] = pd.DataFrame({"node_id": nz.id, "taz_code": nz.taz_code})
        bridges.append(_bridge("node", nz.id, "district", nz.district_id, "spatial_overlap",
                               np.where(nz._inside, 1.0, 0.5), version))

        pts = []
        for name, hub in (("buildings", "building"), ("plots", "plot")):
            t = inputs.get(name)
            if t is None or len(t) == 0:
                continue
            t = _geo(t, crs)
            if hub == "plot" and inputs.get("buildings") is not None and "plot_id" in inputs["buildings"]:
                t = t[~t.plot_id.isin(inputs["buildings"].plot_id.dropna())]
            rp = t.geometry.representative_point()
            pts.append(pd.DataFrame({"hub": hub, "hub_id": t[f"{hub}_id"].astype(str), "x": rp.x, "y": rp.y,
                                     "_v": _version(t)}))
        if pts:
            a = pd.concat(pts).sort_values(["hub", "hub_id"]).reset_index(drop=True)
            a["address_id"] = np.arange(1, len(a) + 1)
            a = zone_of(a)
            near, dist = nearest_valid_node(a[["x", "y"]].to_numpy(), nodes.rename(columns={"id": "node_id"}))
            # Silver has no postcode; the address_id doubles as SimMobility's integer postcode key.
            out["address"] = pd.DataFrame({"address_id": a.address_id, "postcode": a.address_id,
                                           "taz_code": a.taz_code, "x": a.x, "y": a.y})
            out["postcode_node_map"] = pd.DataFrame({"postcode": a.address_id, "node_id": near, "dist_m": dist})
            for hub, g in a.groupby("hub"):
                bridges.append(_bridge("address", g.address_id, hub, g.hub_id, "exact", 1.0, g._v.iloc[0]))

    out["id_bridge"] = pd.concat(bridges, ignore_index=True)
    return out


def write_supply(tables: dict[str, pd.DataFrame], out_dir: os.PathLike | str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
    return out


# --------------------------------------------------------------------------------------------------------------- import
def read_subtrip_metrics(path: os.PathLike | str) -> pd.DataFrame:
    with open(path, newline="") as f:
        rows = [r[: len(SUBTRIP_COLUMNS)] for r in csv.reader(f) if len(r) >= len(SUBTRIP_COLUMNS)]
    df = pd.DataFrame(rows, columns=SUBTRIP_COLUMNS)
    for c in ("travel_time", "total_distance"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["travel_time"])  # drops a header row if present


def read_link_travel_time(path: os.PathLike | str) -> pd.DataFrame:
    df = pd.read_csv(path, sep=";", header=None, names=LINK_TT_COLUMNS)
    df["travel_time_s"] = pd.to_numeric(df["travel_time_s"], errors="coerce")
    return df.dropna(subset=["travel_time_s"]).astype({"link_id": int, "downstream_link_id": int})


def import_outputs(sim_dir: os.PathLike | str, id_bridge: pd.DataFrame, params: dict | None = None) -> dict:
    """SimMobility output dir -> Gold tables keyed by spine hubs (district_id, road_id) via id_bridge."""
    p = {**DEFAULTS, **(params or {})}
    sim = Path(sim_dir)
    out = {}
    taz = id_bridge[id_bridge.source == f"{SRC}.taz"].set_index("source_id")["hub_id"]
    st = read_subtrip_metrics(sim / "subtrip_metrics.csv")
    st["o_district"] = st.origin_taz.astype(str).map(taz)
    st["d_district"] = st.destination_taz.astype(str).map(taz)
    st = st.dropna(subset=["o_district", "d_district"])
    st["travel_time_min"] = st.travel_time * 60.0  # TravelMetric::getTimeDiffHours -> hours
    st["distance_km"] = st.total_distance / 1000.0
    both = pd.concat([st, st.assign(mode="ALL")])
    parts = []
    for direction, col in (("origin", "o_district"), ("destination", "d_district")):
        g = both.groupby([col, "mode"]).agg(subtrips=("subtrip_id", "size"),
                                            mean_travel_time_min=("travel_time_min", "mean"),
                                            mean_distance_km=("distance_km", "mean"))
        parts.append(g.rename_axis(["district_id", "mode"]).reset_index().assign(direction=direction))
    out["district_trips"] = pd.concat(parts, ignore_index=True)[
        ["district_id", "mode", "direction", "subtrips", "mean_travel_time_min", "mean_distance_km"]]

    trips = st.sort_values(["trip_id", "start_time"]).groupby("trip_id").agg(
        o=("o_district", "first"), d=("d_district", "last"), tt=("travel_time_min", "sum"))
    thr = float(p["accessibility_threshold_min"])
    od = trips.groupby(["o", "d"]).tt.mean().reset_index()
    acc = trips.groupby("o").agg(trips=("tt", "size"), mean_trip_time_min=("tt", "mean"),
                                 share_within_threshold=("tt", lambda s: float((s <= thr).mean())))
    acc["reachable_districts"] = od[od.tt <= thr].groupby("o").size().reindex(acc.index).fillna(0).astype(int)
    acc["threshold_min"] = thr
    out["district_accessibility"] = acc.rename_axis("district_id").reset_index()

    lt_path = sim / "link_travel_time.csv"
    if lt_path.exists():
        roads = id_bridge[id_bridge.source == f"{SRC}.link"].set_index("source_id")["hub_id"]
        lt = read_link_travel_time(lt_path)
        g = lt.groupby(["link_id", "start_time", "end_time"]).agg(
            travel_time_s=("travel_time_s", "mean"), downstream_links=("downstream_link_id", "nunique")).reset_index()
        g["road_id"] = g.link_id.astype(str).map(roads)
        out["road_travel_times"] = g.dropna(subset=["road_id"])[
            ["road_id", "link_id", "start_time", "end_time", "travel_time_s", "downstream_links"]]
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish(lake_root, sim_dir, scenario: str = "baseline", params: dict | None = None) -> dict:
    """Import an external run into Gold with the spine's Write-Audit-Publish + OpenLineage conventions."""
    from spine import transform as T
    from spine.lake import Lake

    started = dt.datetime.now(dt.UTC)
    lake, sim = Lake(lake_root), Path(sim_dir)
    desc = T.load_descriptor(Path(__file__).with_name("transform.yaml"))
    params = T.resolve_params(desc, params)
    bridge = lake.silver_read("id_bridge", scenario)
    outputs = import_outputs(sim, bridge, params)
    specs = {o["name"]: o for o in desc.data["outputs"]}
    gdir = lake.gold_dir(scenario, desc.id)
    stage = gdir / "_staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    audit = {"transform": desc.id, "scenario": scenario, "checks": []}
    try:
        for name, df in outputs.items():
            T.check_gold(name, df, specs[name])
            audit["checks"].append({"output": name, "rows": len(df), "passed": True})
            T.write_table(df, stage / f"{name}.parquet")
    except Exception as e:
        audit["checks"].append({"passed": False, "error": str(e)})
        (stage / "_audit.json").write_text(json.dumps(audit, indent=2))
        raise
    lineage_out = []
    for name in outputs:
        dest = gdir / f"{name}.parquet"
        os.replace(stage / f"{name}.parquet", dest)
        lineage_out.append({"namespace": "lake.gold", "name": f"{desc.id}/{name}",
                            "facets": {"scenario": scenario, "rows": len(outputs[name]), "sha256": _sha256(dest)}})
    (gdir / "_audit.json").write_text(json.dumps(audit, indent=2))
    shutil.rmtree(stage)
    files = {f.name: _sha256(f) for f in sorted(sim.glob("*.csv"))}
    lineage_in = [{"namespace": "lake.silver", "name": "id_bridge", "facets": {"scenario": scenario, "rows": len(bridge)}},
                  {"namespace": "external.simmobility", "name": sim.name, "facets": {"files": files}}]
    event = T.run_event(descriptor=desc.data, descriptor_dir=desc.dir, scenario=scenario, params=params,
                        inputs=lineage_in, outputs=lineage_out, started=started)
    (gdir / "_lineage.json").write_text(json.dumps(event, indent=2, default=str))
    return event


# ------------------------------------------------------------------------------------------------------------------ cli
def _silver(lake, name, scenario):
    try:
        return lake.silver_read(name, scenario)
    except FileNotFoundError:
        return None


def write_bridge(lake, rows: pd.DataFrame, scenario: str = "baseline") -> Path:
    """Replace the simmobility.* rows of Silver id_bridge in `scenario` only. Baseline is rewritten in place; any other
    scenario gets an overlay (new/changed rows upserted, simmobility.* rows it no longer produces marked deleted), so
    a scenario export never leaks into baseline or sibling scenarios."""
    old = _silver(lake, "id_bridge", scenario)
    ours = old.source.str.startswith(f"{SRC}.") if old is not None else None
    if scenario == "baseline":
        return lake.silver_write("id_bridge", pd.concat([None if old is None else old[~ours], rows], ignore_index=True))
    if old is None:
        raise FileNotFoundError(f"scenario '{scenario}' has no id_bridge to overlay; materialise baseline Silver "
                                "id_bridge first (e.g. `export --write-bridge` without --scenario)")
    key = ["source", "source_id"]
    stale = old[ours & ~old.set_index(key).index.isin(rows.set_index(key).index)]
    overlay = pd.concat([rows.assign(_deleted=False), stale.assign(_deleted=True)], ignore_index=True)
    return lake.scenario_set(scenario, "id_bridge", overlay)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="simmobility.mid_term adapter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--lake", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--scenario", default="baseline")
    e.add_argument("--write-bridge", action="store_true", help="merge simmobility.* rows into Silver id_bridge")
    pb = sub.add_parser("publish")
    pb.add_argument("--lake", required=True)
    pb.add_argument("--sim-output", required=True)
    pb.add_argument("--scenario", default="baseline")
    a = ap.parse_args(argv)
    from spine.lake import Lake

    lake = Lake(a.lake)
    if a.cmd == "export":
        names = ("roads", "districts", "buildings", "plots", "id_bridge")
        tables = export_supply({n: _silver(lake, n, a.scenario) for n in names})
        write_supply(tables, a.out)
        if a.write_bridge:
            write_bridge(lake, tables["id_bridge"], a.scenario)
        print(json.dumps({k: len(v) for k, v in tables.items()}))
    else:
        print(json.dumps(publish(a.lake, a.sim_output, a.scenario)["run"], default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
