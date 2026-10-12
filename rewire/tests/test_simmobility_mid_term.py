"""Tests for the SimMobility Mid-Term rewire (descriptor + mapping; SimMobility itself is external)."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

gpd = pytest.importorskip("geopandas")
from shapely.geometry import LineString, Point, Polygon

HERE = Path(__file__).resolve().parent
TDIR = HERE.parent / "simmobility.mid_term"
spec = importlib.util.spec_from_file_location("simmobility_mid_term_adapter", TDIR / "adapter.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
VALID_HUBS = {"plot", "building", "district", "road", "station"}


def silver_fixture():
    """Tiny Jurong East-like layout in SVY21 (EPSG:3414): a T-junction, two districts, two buildings, one empty plot."""
    x0, y0 = 17000.0, 35000.0
    roads = gpd.GeoDataFrame({
        "road_id": ["R1", "R2", "R3"], "road_class": ["primary", "primary", "tertiary"],
        "name": ["Jurong East St A", "Jurong East St A", "Jurong East St B"], "source_version": "osm-2026-10",
        "geometry": [LineString([(x0, y0), (x0 + 500, y0)]), LineString([(x0 + 500, y0), (x0 + 1000, y0)]),
                     LineString([(x0 + 500, y0), (x0 + 500, y0 + 400), (x0 + 500, y0 + 800)])]}, crs="EPSG:3414")
    districts = gpd.GeoDataFrame({"district_id": ["D-WEST", "D-EAST"], "source_version": "mp2019",
                                  "geometry": [Polygon([(x0 - 100, y0 - 500), (x0 + 500, y0 - 500), (x0 + 500, y0 + 900),
                                                        (x0 - 100, y0 + 900)]),
                                               Polygon([(x0 + 500, y0 - 500), (x0 + 1100, y0 - 500),
                                                        (x0 + 1100, y0 + 900), (x0 + 500, y0 + 900)])]},
                                 crs="EPSG:3414")
    buildings = gpd.GeoDataFrame({"building_id": ["B1", "B2"], "plot_id": ["P1", "P2"], "source_version": "osm-2026-10",
                                  "geometry": [Point(x0 + 50, y0 + 30).buffer(10), Point(x0 + 900, y0 + 40).buffer(10)]},
                                 crs="EPSG:3414")
    plots = gpd.GeoDataFrame({"plot_id": ["P1", "P2", "P3"], "source_version": "mp2019",
                              "geometry": [Point(x0 + 50, y0 + 30).buffer(30), Point(x0 + 900, y0 + 40).buffer(30),
                                           Point(x0 + 560, y0 + 780).buffer(30)]}, crs="EPSG:3414")
    return {"roads": roads, "districts": districts, "buildings": buildings, "plots": plots}


def test_descriptor_validates():
    from spine.cli import main

    assert main(["validate", str(TDIR / "transform.yaml")]) in (0, None)


def test_descriptor_is_honest_about_runtime_and_licence():
    from spine.transform import NotRunnable, invoke, load_descriptor

    d = load_descriptor(TDIR / "transform.yaml")
    assert d.data["runtime"]["type"] == "external"
    assert d.data["upstream"]["commercial_use"] == "restricted"
    assert all(not i["source"].startswith("bronze:") for i in d.data["inputs"])
    with pytest.raises(NotRunnable):
        invoke(d, {}, {})


def test_golden_postcode_to_node_matches_upstream_reference():
    """Frozen from upstream's own Build/Postcode_To_Node reference output (see golden/make_postcode_fixture.py)."""
    g = TDIR / "golden" / "postcode_to_node_jurong"
    nodes = pd.read_parquet(g / "inputs" / "nodes.parquet")
    pc = pd.read_parquet(g / "inputs" / "postcodes.parquet")
    want = pd.read_parquet(g / "expected" / "postcode_node_map.parquet").set_index("sla_postcode").loc[pc.sla_postcode]
    node, dist = A.nearest_valid_node(pc[["x", "y"]].to_numpy(), nodes)
    assert len(pc) == 2847
    np.testing.assert_array_equal(node, want.nearest_node.to_numpy())
    np.testing.assert_allclose(dist, want.nearest_node_dist.to_numpy(), atol=1e-6)


def test_export_supply_tables_and_crs():
    s = silver_fixture()
    t = A.export_supply(s)
    assert len(t["node"]) == 4 and len(t["link"]) == 6 and len(t["segment"]) == 6
    assert set(t["node"].node_type) == {1, 2}  # three dead ends + one T-junction
    assert t["node"].node_type.value_counts()[2] == 1
    assert list(t["link"].columns) == ["id", "road_type", "category", "from_node", "to_node", "road_name", "tags", "osmid"]
    assert list(t["segment"].columns) == ["id", "link_id", "sequence", "num_lanes", "capacity", "max_speed", "tags",
                                          "link_category"]
    assert set(t["link"].category) == {2, 4} and set(t["segment"].max_speed) == {80, 50}
    back = gpd.GeoSeries(gpd.points_from_xy(t["node"].x, t["node"].y), crs=A.SIMMOB_CRS).to_crs("EPSG:3414")
    ends = {(round(x, 3), round(y, 3)) for g in s["roads"].geometry for x, y in (g.coords[0], g.coords[-1])}
    assert {(round(p.x, 3), round(p.y, 3)) for p in back} == ends
    assert t["node"].x.between(3.4e5, 4.0e5).all()  # really UTM 48N, not SVY21
    assert dict(zip(t["taz"].district_id, t["taz"].taz_code)) == {"D-EAST": 1, "D-WEST": 2}
    assert len(t["address"]) == 3  # B1, B2 + P3 (plot without a building)
    assert set(t["postcode_node_map"].postcode) == set(t["address"].postcode)


def test_id_bridge_rows_cover_every_generated_id():
    t = A.export_supply(silver_fixture())
    b = t["id_bridge"]
    assert not b.duplicated(["source", "source_id"]).any()
    assert set(b.hub) <= VALID_HUBS
    assert set(b.match_method) <= {"exact", "spatial_overlap", "manual"}
    for src, table in [("link", "link"), ("segment", "segment"), ("node", "node"), ("taz", "taz")]:
        ids = t[table]["taz_code" if table == "taz" else "id"].astype(str)
        assert set(b[b.source == f"simmobility.{src}"].source_id) == set(ids)
    seg = b[b.source == "simmobility.segment"].set_index("source_id").hub_id
    link = b[b.source == "simmobility.link"].set_index("source_id").hub_id
    for r in t["segment"].itertuples():
        assert seg[str(r.id)] == link[str(r.link_id)]
    addr = b[b.source == "simmobility.address"]
    assert set(zip(addr.hub, addr.hub_id)) == {("building", "B1"), ("building", "B2"), ("plot", "P3")}
    nodes = b[b.source == "simmobility.node"]
    assert set(nodes.hub_id) <= {"D-WEST", "D-EAST"}
    assert b.source_version.notna().all()
    from spine.validate import check_silver

    check_silver("id_bridge", b)


def test_ids_stay_stable_when_bridge_is_fed_back():
    s = silver_fixture()
    first = A.export_supply(s)
    s2 = dict(s)
    extra = gpd.GeoDataFrame({"road_id": ["R0"], "road_class": ["secondary"], "name": ["New Rd"],
                              "source_version": "osm-2026-11",
                              "geometry": [LineString([(16500, 35000), (17000, 35000)])]}, crs="EPSG:3414")
    s2["roads"] = pd.concat([extra, s["roads"]], ignore_index=True)
    s2["id_bridge"] = first["id_bridge"]
    second = A.export_supply(s2)

    def links(t):
        b = t["id_bridge"]
        return b[b.source == "simmobility.link"].groupby("hub_id").source_id.apply(set).to_dict()

    l1, l2 = links(first), links(second)
    for road in ("R1", "R2", "R3"):
        assert l1[road] == l2[road]
    assert l2["R0"].isdisjoint(set().union(*l1.values()))
    assert first["taz"].equals(second["taz"])


def write_sim_outputs(d: Path, t):
    """Files in the formats SimMobility writes (Person.cpp subtrip metrics, TravelTimeManager link TT)."""
    east, west = (t["taz"].set_index("district_id").taz_code[k] for k in ("D-EAST", "D-WEST"))
    cbd = ",".join(["0"] * 8)
    rows = [  # person, trip, subtrip, otype, oid, otaz, dtype, did, dtaz, mode, start, end, tt_s, dist_m, cbd...  (tt in hours)
        f"p1,p1_1,1,NODE,1,{west},NODE,4,{east},Car,08:00:00,08:15:00,0.25,6000,{cbd}",
        f"p1,p1_2,1,NODE,4,{east},NODE,1,{west},Car,18:00:00,18:30:00,0.5,6000,{cbd}",
        f"p2,p2_1,1,NODE,1,{west},BUS_STOP,9,{east},Walk,07:00:00,07:15:00,0.25,500,{cbd}",
        f"p2,p2_1,2,BUS_STOP,9,{west},NODE,4,{east},BusTravel,07:15:00,08:00:00,0.75,9500,{cbd}",
    ]
    (d / "subtrip_metrics.csv").write_text("\n".join(rows) + "\n")
    lid = int(t["link"].id.iloc[0])
    (d / "link_travel_time.csv").write_text(
        f"{lid};{lid + 2};08:00:00;08:04:59;30\n{lid};{lid + 4};08:00:00;08:04:59;40\n{lid};{lid + 2};08:05:00;08:09:59;35\n")
    return lid


def test_import_outputs_to_gold(tmp_path):
    t = A.export_supply(silver_fixture())
    lid = write_sim_outputs(tmp_path, t)
    g = A.import_outputs(tmp_path, t["id_bridge"])
    dtr = g["district_trips"].set_index(["district_id", "mode", "direction"])
    assert dtr.loc[("D-WEST", "ALL", "origin"), "subtrips"] == 3
    assert dtr.loc[("D-EAST", "ALL", "destination"), "subtrips"] == 3
    assert dtr.loc[("D-WEST", "Car", "origin"), "mean_travel_time_min"] == pytest.approx(15.0)
    assert dtr.loc[("D-WEST", "BusTravel", "origin"), "mean_distance_km"] == pytest.approx(9.5)
    assert g["district_trips"].notna().all().all()
    acc = g["district_accessibility"].set_index("district_id")
    assert acc.loc["D-WEST", "trips"] == 2  # p1_1 and p2_1 (walk + bus legs summed)
    assert acc.loc["D-WEST", "mean_trip_time_min"] == pytest.approx((15 + 60) / 2)
    assert acc.loc["D-WEST", "share_within_threshold"] == pytest.approx(0.5)
    assert acc.loc["D-EAST", "reachable_districts"] == 1
    rt = g["road_travel_times"].set_index(["link_id", "start_time"])
    assert rt.loc[(lid, "08:00:00"), "travel_time_s"] == pytest.approx(35.0)
    assert rt.loc[(lid, "08:00:00"), "downstream_links"] == 2
    assert rt.loc[(lid, "08:00:00"), "road_id"] == t["id_bridge"].set_index("source").loc["simmobility.link"].set_index(
        "source_id").hub_id[str(lid)]


def test_publish_write_audit_publish_with_lineage(tmp_path):
    from spine.lake import Lake

    lake = Lake(tmp_path / "lake")
    t = A.export_supply(silver_fixture())
    lake.silver_write("id_bridge", t["id_bridge"])
    sim = tmp_path / "run1"
    sim.mkdir()
    write_sim_outputs(sim, t)
    ev = A.publish(lake.root, sim)
    gdir = lake.gold_dir("baseline", "simmobility.mid_term")
    for name in ("district_trips", "district_accessibility", "road_travel_times"):
        assert len(lake.gold_read("simmobility.mid_term", name)) > 0
    assert not (gdir / "_staging").exists()
    assert all(c["passed"] for c in json.loads((gdir / "_audit.json").read_text())["checks"])
    lin = json.loads((gdir / "_lineage.json").read_text())
    assert lin["run"]["runId"] == ev["run"]["runId"]
    ext = next(i for i in lin["inputs"] if i["namespace"] == "external.simmobility")
    assert set(ext["facets"]["files"]) == {"subtrip_metrics.csv", "link_travel_time.csv"}


def test_cli_export_writes_csvs_and_bridge(tmp_path):
    from spine.lake import Lake

    lake = Lake(tmp_path / "lake")
    for k, v in silver_fixture().items():
        lake.silver_write(k, v)
    assert A.main(["export", "--lake", str(lake.root), "--out", str(tmp_path / "supply"), "--write-bridge"]) == 0
    for f in ("node", "link", "segment", "segment_polyline", "link_polyline", "taz", "node_taz_map", "address",
              "postcode_node_map", "id_bridge"):
        assert (tmp_path / "supply" / f"{f}.csv").exists()
    assert lake.silver_read("id_bridge").source.str.startswith("simmobility.").all()


def _link_rows(bridge):
    return bridge[bridge.source == "simmobility.link"]


def test_cli_export_scenario_bridge_stays_in_scenario(tmp_path):
    """--scenario S --write-bridge must overlay id_bridge in S only (LAYERS.md Scenarios row), never touch baseline."""
    from spine.lake import Lake

    lake = Lake(tmp_path / "lake")
    s = silver_fixture()
    for k, v in s.items():
        lake.silver_write(k, v)
    assert A.main(["export", "--lake", str(lake.root), "--out", str(tmp_path / "base"), "--write-bridge"]) == 0
    base_before = lake.silver_read("id_bridge").sort_values(["source", "source_id"]).reset_index(drop=True)
    assert len(_link_rows(base_before)) == 6

    lake.scenario_create("new_road", description="adds RNEW, removes R3")
    new = gpd.GeoDataFrame({"road_id": ["RNEW"], "road_class": ["secondary"], "name": ["New Rd"],
                            "source_version": "osm-2026-11",
                            "geometry": [LineString([(16500, 35000), (17000, 35000)])]}, crs="EPSG:3414")
    lake.scenario_set("new_road", "roads", new)
    lake.scenario_set("new_road", "roads", pd.DataFrame({"road_id": ["R3"], "_deleted": [True]}))
    assert A.main(["export", "--lake", str(lake.root), "--out", str(tmp_path / "scen"), "--scenario", "new_road",
                   "--write-bridge"]) == 0

    base_after = lake.silver_read("id_bridge").sort_values(["source", "source_id"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(base_before, base_after)
    assert "RNEW" not in set(base_after.hub_id)

    scen = lake.silver_read("id_bridge", "new_road")
    links = _link_rows(scen).groupby("hub_id").source_id.apply(set).to_dict()
    base_links = _link_rows(base_before).groupby("hub_id").source_id.apply(set).to_dict()
    assert set(links) == {"R1", "R2", "RNEW"}  # R3 rows are deleted in the scenario overlay only
    assert links["R1"] == base_links["R1"] and links["R2"] == base_links["R2"]
    assert links["RNEW"].isdisjoint(set().union(*base_links.values()))
    exported = pd.read_csv(tmp_path / "scen" / "id_bridge.csv", dtype=str)
    assert set(map(tuple, scen[["source", "source_id"]].astype(str).to_numpy())) >= \
        set(map(tuple, exported[["source", "source_id"]].to_numpy()))
    assert not scen.duplicated(["source", "source_id"]).any()

    # re-export is idempotent and publish for the scenario resolves against the scenario bridge
    assert A.main(["export", "--lake", str(lake.root), "--out", str(tmp_path / "scen2"), "--scenario", "new_road",
                   "--write-bridge"]) == 0
    assert _link_rows(lake.silver_read("id_bridge", "new_road")).groupby("hub_id").source_id.apply(set).to_dict() == links
    sim = tmp_path / "run_s"
    sim.mkdir()
    rnew_link = min(int(i) for i in links["RNEW"])
    (sim / "subtrip_metrics.csv").write_text("")
    (sim / "link_travel_time.csv").write_text(f"{rnew_link};{rnew_link};08:00:00;08:04:59;30\n")
    A.main(["publish", "--lake", str(lake.root), "--sim-output", str(sim), "--scenario", "new_road"])
    rt = lake.gold_read("simmobility.mid_term", "road_travel_times", "new_road")
    assert set(rt.road_id) == {"RNEW"}
    assert not (lake.gold_dir("baseline", "simmobility.mid_term")).exists()


def test_cli_export_scenario_bridge_needs_baseline_bridge(tmp_path):
    from spine.lake import Lake

    lake = Lake(tmp_path / "lake")
    for k, v in silver_fixture().items():
        lake.silver_write(k, v)
    lake.scenario_create("s1")
    with pytest.raises(FileNotFoundError, match="baseline"):
        A.main(["export", "--lake", str(lake.root), "--out", str(tmp_path / "o"), "--scenario", "s1", "--write-bridge"])
    assert not lake.silver_path("id_bridge").exists()
