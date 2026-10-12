# rewire/ — SimMobility Mid-Term on the sg-urban-spine

"Rewire, don't rewrite": this directory plugs SimMobility Mid-Term into
[sg-urban-spine](https://github.com/tetratorus/sg-urban-spine) as **descriptor + mapping only**. No SimMobility
source file was changed, and SimMobility is not built, run, packaged or redistributed by the spine.

```
rewire/
  simmobility.mid_term/
    transform.yaml        spine descriptor (runtime: external, commercial_use: restricted, readiness RL3)
    adapter.py            export Silver -> SimMobility supply CSVs + id_bridge;  publish SimMobility outputs -> Gold
    golden/
      make_postcode_fixture.py     freezes the fixture below from upstream's own checked-in files
      postcode_to_node_jurong/     2,847 Jurong East SLA postcodes + 6,339 nodes -> upstream reference node map
  tests/test_simmobility_mid_term.py
```

## Licence (read first)

`license.txt` (SimMobility Version Control License, MIT 2017): free for **non-commercial** use; the Standard Version or
any Package "cannot be commercially sold"; other commercial use needs a licence from MIT/SMART. The descriptor therefore
sets `upstream.commercial_use: restricted`. The spine and this adapter only exchange data files with an operator's own
SimMobility install; do not ship SimMobility binaries, sources or its data with a spine deployment.

## How it runs (runtime `external`)

SimMobility Mid-Term is a C++ program that reads the network, zones and population from a **PostgreSQL** schema
through stored procedures (`get_nodes()`, `get_links()`, `get_road_segments()`, `get_segment_polylines()`, `get_lanes()`,
`get_lane_connectors()`, `get_postcode_node_map()`, `getaddresses()`, ... in `dev/Basic/data/simrun_MidTerm.xml`) and
writes CSV statistics. That needs a Linux build (CMake, Boost, libpq, Lua, XercesC), a populated database with
credentials (`dev/Basic/data/simulation.xml` credential files — never commit them) and Singapore population/preday
inputs that are not in this repo. So `spine run simmobility.mid_term` raises `NotRunnable` by design; the three steps are:

```bash
pip install 'git+https://github.com/tetratorus/sg-urban-spine@main' geopandas
# 1. Silver -> SimMobility supply CSVs (EPSG:32648) + id_bridge rows
python rewire/simmobility.mid_term/adapter.py export --lake $SPINE_LAKE --out supply/ [--scenario S] [--write-bridge]
# 2. operator: generate lanes/connectors/turning groups with dev/tools/roadNetworkFromOpenStreetMap_Python3.6,
#    load into the SimMobility DB (uploadToDb.py), then:  ./SimMobility_Medium data/simulation.xml data/simrun_MidTerm.xml
# 3. SimMobility outputs -> Gold (Write-Audit-Publish + OpenLineage _lineage.json, like `spine run`)
python rewire/simmobility.mid_term/adapter.py publish --lake $SPINE_LAKE --sim-output <run dir> [--scenario S]
```

### Silver -> SimMobility (export)

| Silver input | SimMobility table (columns as written by upstream `network.py`) | Rule |
|---|---|---|
| `roads` (`road_id`, `geometry`, `road_class`, `name`) | `node`, `link`, `link_polyline`, `segment`, `segment_polyline` | end-points within `node_snap_m` share a node; one link per direction (`two_way`), one segment per link; `road_class` read as OSM `highway` -> category/road type/speed per `process_osm.py` + `metadata/LinkCat_Roadtype_LaneWidth.csv`; upstream defaults 1 lane, 1000 veh/h |
| `districts` | `taz`, `node_taz_map` | one TAZ per district; nodes join the containing (else nearest) district |
| `buildings`, `plots` without a building | `address`, `postcode_node_map` | representative point; nearest eligible node using the upstream `02_Postcode_to_node.py` rule (skip node types 1/8/9 and bus termini) |
| `id_bridge` (optional) | — | prior `simmobility.*` rows keep link and TAZ IDs stable between exports |

Lanes, lane connectors, turning groups and PT stops are **not** generated here (left to upstream tooling / the upstream
SG network).

### CRS

Spine Silver is EPSG:3414 (SVY21). The upstream SG network tooling works in UTM 48N (`project.py`, EPSG:32648), and the
checked-in SG node/postcode files use UTM 48N coordinates. Export reprojects 3414 -> 32648 (tests check a < 1 mm round
trip); nothing is silently assumed to be the same grid. Override with `simmobility_crs` if your DB uses another CRS.

### SimMobility -> Gold (publish)

| SimMobility output | Gold table (key) | Notes |
|---|---|---|
| `subtrip_metrics.csv` (`Person::serializeSubTripChainItemTravelTimeMetrics`) | `district_trips` (`district_id, mode, direction`) | origin/destination TAZ -> district via id_bridge; `travel_time` is hours (`TravelMetric::getTimeDiffHours`) -> minutes; distance m -> km; `mode=ALL` row per district |
| same | `district_accessibility` (`district_id`) | per origin district: trips, mean trip time (sum of subtrip times), share within `accessibility_threshold_min`, destination districts reachable within it |
| `link_travel_time.csv` (`LinkTravelTime::dumpTravelTimesToFile`, `;`-separated, seconds) | `road_travel_times` (`link_id, start_time`) | mean over downstream links, `road_id` via id_bridge |

## id_bridge plan (GERS pattern — no new hub keys)

Every SimMobility ID is a row in Silver `id_bridge` (`source, source_id -> hub, hub_id, match_method, confidence,
source_version`); the spine hubs stay `road`/`district`/`building`/`plot`/`station`.

| `source` | SimMobility ID | `hub` | `match_method` / confidence | Notes |
|---|---|---|---|---|
| `simmobility.link` | `link.id` (one per direction) | `road` | exact / 1.0 | reused from prior bridge rows; new roads get IDs above the previous max |
| `simmobility.segment` | `segment.id` = `link.id*10+1` | `road` | exact / 1.0 | lanes follow the upstream `segment*100+i` convention when generated upstream |
| `simmobility.node` | `node.id` | `district` | spatial_overlap / 1.0 inside, 0.5 nearest | the spine has no node hub; road incidence comes from `link.from_node/to_node` |
| `simmobility.taz` | `taz_code` | `district` | exact / 1.0 | stable across exports via prior rows |
| `simmobility.address` | `address_id` (= integer postcode) | `building` or `plot` | exact / 1.0 | Silver has no postcode, so the address id doubles as SimMobility's postcode key |
| `simmobility.pt_stop` (planned) | bus stop / MRT platform codes | `station` | exact on LTA codes, else spatial_overlap | not generated yet |
| existing upstream SG network IDs (planned) | node/link/segment/TAZ IDs in the upstream DB | `road` / `district` | spatial_overlap | needs the upstream zone polygons and segment polylines, which are not in this repo |

## Tests and golden

```bash
pip install 'git+https://github.com/tetratorus/sg-urban-spine@main' geopandas pytest
spine validate rewire/simmobility.mid_term/transform.yaml
pytest rewire/tests
```

`spine test` reports "no golden cases" for this descriptor: an `external` runtime cannot be invoked by the spine, and
no real Mid-Term output is available to freeze. The golden that *is* frozen comes from the upstream's own reference
results: `golden/postcode_to_node_jurong` holds the 2,847 SLA postcodes in a Jurong East box (UTM 48N
x 350–356 km, y 145–150 km) and `Build/Postcode_To_Node/postcode_to_nereastnode.csv` as expected output; the adapter
matches all 2,847 exactly. Island-wide it matches 125,327 / 126,358 (99.18%); every mismatch is a reference node that the
checked-in `node_to_zone.csv` marks as excluded (types 1/8/9 or bus terminus), i.e. the reference was made against an
older node table. The other tests use a small SVY21 fixture and hand-written files in the exact SimMobility output
formats.

## Readiness (NOAA RL 3)

What was run: `spine validate`; pytest (export, CRS, id_bridge, ID stability, output parsing, Write-Audit-Publish +
lineage); the postcode->node comparison against upstream reference data. What was **not** run: building or running
SimMobility, any database load, any real Mid-Term output. See `readiness.gaps` in `transform.yaml`.

## Upstream changes

None. All work lives in `rewire/`.

## Repo's own tests

The upstream repo has no automated test suite that runs without a SimMobility build and database: there is no
CMake/Boost/libpq/XercesC toolchain on this machine, no PostgreSQL schema, and `dev/tools/.../test_outputs.py` reads
outputs of an OSM download run. Those were not run; the rewire tests above are self-contained.
